from datetime import UTC, timedelta
from fnmatch import fnmatch

from reviewer.context.models import Budget, CodeContext, ContextBundle
from reviewer.context.redaction import Redactor
from reviewer.context.requirements import collect
from reviewer.services.forge.gitlab import StaleReview
from reviewer.services.symbols.index import SymbolIndex
from reviewer.telemetry.activity import activity


@activity("tool", "Collect review context")
async def build(
    review,
    mr,
    wt,
    git,
    forge,
    issues,
    docs,
    scanner,
    config,
    previous=None,
    overrides=None,
):
    import asyncio

    base = await git.merge_base(wt, mr.target_branch, review.head_sha)

    @activity("tool", "Read changed code")
    async def code():
        files = await git.diff(
            wt, base, review.head_sha, context_lines=config.review.context_lines
        )
        remote = await forge.get_changed_paths(mr.project_id, mr.iid)
        if set(remote) != {f.path for f in files}:
            raise StaleReview("Local and forge diff inventories differ")
        return files

    @activity("tool", "Collect requirements")
    async def requirements():
        from reviewer.context.linkage import resolve
        from reviewer.context.models import Linkage
        from reviewer.services.docs.confluence import DocumentContext
        from reviewer.services.issues.jira import EpicContext, IssueContext

        commits = (
            (
                await git.command(
                    "log",
                    "--format=%s",
                    f"{base}..{review.head_sha}",
                    cwd=wt.mirror,
                    limit=100000,
                )
            )
            .decode()
            .splitlines()
        )
        key = (
            overrides.issue_key
            if overrides and overrides.issue_key
            else resolve(mr, config.issue_tracker.project_keys, commits).issue_key
        )
        # Cached requirements describe the previous run's inputs; hand-supplied
        # pages or an epic are new inputs, so they force a fresh collection.
        reusable = not overrides or not (overrides.document_urls or overrides.epic_key)
        if (
            previous
            and key
            and reusable
            and hasattr(issues, "updated")
            and previous.get("issue_updated")
            and previous.get("config_hash")
            == __import__("hashlib")
            .sha256(config.model_dump_json().encode())
            .hexdigest()
        ):
            try:
                if (
                    await issues.updated(key) == previous["issue_updated"]
                    and previous["bundle"]["linkage"]["issue_key"] == key
                ):
                    old = previous["bundle"]
                    return (
                        Linkage.model_validate(old["linkage"]),
                        IssueContext.model_validate(old["issue"]),
                        EpicContext.model_validate(old["epic"])
                        if old.get("epic")
                        else None,
                        [
                            DocumentContext.model_validate(d)
                            for d in old.get("documents", [])
                        ],
                        [],
                    )
            except Exception:
                pass
        return await collect(mr, config, issues, docs, commits, overrides)

    files, requirements = await asyncio.gather(code(), requirements())
    linkage, issue, epic, pages, degradations = requirements
    for f in files:
        f.is_excluded = any(
            fnmatch(f.path, p) or fnmatch("/" + f.path, p)
            for p in config.review.exclude
        )
        if f.is_excluded:
            degradations.append("excluded:" + f.path)
    matches = await scanner.scan(files)
    redactor = Redactor(matches)
    started = review.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    bundle = ContextBundle(
        review_id=review.id,
        mr=mr,
        linkage=linkage,
        issue=issue,
        epic=epic,
        documents=pages,
        code=CodeContext(
            merge_base_sha=base,
            head_sha=review.head_sha,
            target_branch=mr.target_branch,
            files=files,
            worktree_path=wt.path,
            total_changed_lines=sum(
                line.kind != "context" for f in files for line in f.lines
            ),
        ),
        budget=Budget(
            token_ceiling=config.review.token_ceiling,
            deadline_at=started + timedelta(seconds=config.review.timeout_s),
            model_tier={"strong": "configured", "fast": "configured"},
        ),
        degradations=list(dict.fromkeys(degradations)),
    )
    if bundle.code.total_changed_lines > config.review.max_changed_lines:
        bundle.degradations.append("triage_mode")
    from reviewer.services.secrets.scanner import findings

    secret_findings = findings(matches, bundle)
    # Sanitize the complete bundle before prompts, persistence, or publication.
    bundle = ContextBundle.model_validate(
        redactor.object(bundle.model_dump(mode="json"))
    )
    symbols = SymbolIndex()
    for file in files:
        if file.change_type == "deleted":
            continue
        try:
            symbols.add(file.path, await git.read_file(wt, file.path))
        except Exception:
            pass

    from reviewer.context.code_cache import ScannedCodeCache

    code_cache = ScannedCodeCache(git, wt, scanner, redactor)

    @activity("tool", "Read requested code context")
    async def context_provider(requests):
        output = {}
        for request in requests[:5]:
            paths = [request.target]
            if request.kind == "symbol":
                paths = [
                    p
                    for p, syms in symbols.symbols.items()
                    if any(s["name"] == request.target for s in syms)
                ][:3]
            elif request.kind in {"callers", "tests_for"}:
                paths = [
                    f.path
                    for f in files
                    if (request.target in f.path or "test" in f.path)
                ][:3]
            for path in paths:
                try:
                    output[path] = await code_cache.read(path)
                except Exception:
                    output[path] = "unavailable"
        return output

    return bundle, redactor, symbols, secret_findings, context_provider
