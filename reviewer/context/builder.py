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
    models=None,
):
    """`models` is the resolved role -> model ID map recorded on the budget."""
    import asyncio

    base = await git.merge_base(wt, mr.target_branch, review.head_sha)

    @activity("tool", "Read changed code")
    async def code():
        files = await git.diff(
            wt, base, review.head_sha, context_lines=config.review.context_lines
        )
        remote = await forge.get_changed_paths(mr.project_id, mr.iid)
        # Compare affected paths, independent of rename pairing/thresholds.
        local_paths = {f.path for f in files}
        local_paths.update(f.old_path for f in files if f.old_path)
        if set(remote) != local_paths:
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
            model_tier=models or {},
        ),
        degradations=list(dict.fromkeys(degradations)),
    )
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
            source = await git.read_file(wt, file.path)
            symbols.add(file.path, source)
            supplied = next(f for f in bundle.code.files if f.path == file.path)
            supplied.total_lines = len(source.splitlines())
            supplied.symbol_ranges = [
                (s["start"], s["end"]) for s in symbols.symbols.get(file.path, [])
            ]
        except Exception:
            pass

    from reviewer.context.code_cache import ScannedCodeCache
    from reviewer.context.lookup import CodeLookup

    code_cache = ScannedCodeCache(git, wt, scanner, redactor)
    lookup = CodeLookup(git, wt, code_cache, symbols)
    try:
        await attach_scopes(bundle, symbols, code_cache)
    except Exception:
        # Scope is optional context; the diff with its context lines remains,
        # and evidence is still scanned before each verifier batch.
        bundle.degradations.append("scope_unavailable")

    @activity("tool", "Read requested code context")
    async def context_provider(requests):
        output = {}
        for request in requests[:5]:
            try:
                if request.kind == "symbol":
                    found = await lookup.symbol(request.target)
                elif request.kind == "callers":
                    found = await lookup.callers(request.target)
                elif request.kind == "tests_for":
                    found = await lookup.tests_for(request.target)
                else:
                    found = await lookup.file(
                        request.target, request.line_start, request.line_end
                    )
                output.update(found or {request.target: {"unavailable": True}})
            except Exception:
                output[request.target] = {"unavailable": True}
        return output

    # The context planner and the build agent read through the same scanned
    # lookup: whatever they show a model has passed secret scanning first.
    context_provider.lookup = lookup
    return bundle, redactor, symbols, secret_findings, context_provider


# The innermost enclosing definition of a changed line is shown whole when it
# is at most this long; a longer one keeps the diff's own context lines.
SCOPE_LINES = 120


async def attach_scopes(bundle, symbols, code_cache):
    """Widen each change to its enclosing definition with scanned head lines.

    Every reviewed file is read through the scanned cache in one batch, not only
    those that need scope: findings are verified while other units still run,
    and the code a verifier cites is then already scanned before any verifier
    prompt is built, as it was when verification waited for every unit.
    """
    wanted = {}
    reviewed = []
    for file in bundle.code.files:
        if file.is_excluded or file.change_type == "deleted":
            continue
        reviewed.append(file.path)
        present = {line.new_line for line in file.lines if line.new_line is not None}
        changed, following = set(), None
        for line in reversed(file.lines):
            if line.new_line is not None:
                following = line.new_line
                if line.kind == "added":
                    changed.add(line.new_line)
            elif following is not None:
                # A removal sits where the next surviving line now is.
                changed.add(following)
        lines = set()
        for number in changed:
            scope = symbols.enclosing(file.path, number)
            if scope and scope["end"] - scope["start"] < SCOPE_LINES:
                lines.update(range(scope["start"], scope["end"] + 1))
        if lines - present:
            wanted[file.path] = (file, lines - present)
    if not reviewed:
        return
    sources = await code_cache.read_many(reviewed)
    for path, (file, lines) in wanted.items():
        if path not in sources:
            continue
        text = sources[path].splitlines()
        file.scope = {n: text[n - 1] for n in sorted(lines) if n <= len(text)}
