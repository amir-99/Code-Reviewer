import asyncio
import hashlib
import json
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import structlog

from reviewer.agents.base import PROMPTS
from reviewer.agents.build_agent import BuildAgent
from reviewer.agents.severity_triage import SeverityTriageAgent
from reviewer.config.loader import load_project
from reviewer.config.models import ResolvedModel, assignment, resolve
from reviewer.config.schema import ProjectConfig
from reviewer.context.builder import build
from reviewer.context.framing import INJECTION_RULE
from reviewer.decision.engine import decide, status
from reviewer.findings.dedup import deduplicate, fingerprint
from reviewer.findings.models import (
    Finding,
    ProposedFinding,
    Provenance,
    VerificationResult,
)
from reviewer.findings.policy import needs_verification, normalize
from reviewer.findings.validator import change_commits, validate
from reviewer.orchestrator.budget import BudgetTracker
from reviewer.orchestrator.reasons import failure_reason
from reviewer.orchestrator.stages import execute
from reviewer.orchestrator.states import ORDER, TERMINAL
from reviewer.orchestrator.streaming import FileStream
from reviewer.orchestrator.verification import verify_findings
from reviewer.publish.publisher import Publisher
from reviewer.publish.rereview import adopt_identities, full_review, reanchor
from reviewer.services.forge.gitlab import StaleReview
from reviewer.services.llm.client import GatewayClient
from reviewer.store.audit import Audit, blob_store
from reviewer.telemetry.activity import activity, sink, traced_review

logger = structlog.get_logger()

# Findings on pre-existing code that publication can show (as FYIs).
EXISTING_FINDINGS = 2


def frozen_config(data):
    # Every run, including one frozen when the seven-stage deep mode existed,
    # now runs the combined Defect Review; the schema drops the old selector.
    return ProjectConfig.model_validate(data)


class Pipeline:
    def __init__(
        self,
        store,
        forge,
        settings,
        git,
        issues,
        docs,
        scanner,
        static,
        llm_factory=None,
    ):
        self.store, self.forge, self.settings = store, forge, settings
        self.git, self.issues, self.docs, self.scanner, self.static = (
            git,
            issues,
            docs,
            scanner,
            static,
        )
        self.llm_factory = llm_factory
        self.gateway_slots = asyncio.Semaphore(settings.gateway_concurrency)

    @traced_review
    async def run(self, review_id):
        review = await self.store.get(review_id)
        if not review:
            logger.warning("review_not_found", review_id=review_id)
            return
        project = await self.store.project_number(review)
        config = (
            frozen_config(review.execution_config["config"])
            if review.execution_config
            else load_project(self.settings.config_path, project)
        )
        level = min(int(self.settings.milestone[1:]), int(config.milestone[1:]))
        logger.info(
            "pipeline_start",
            review_id=review_id,
            project_id=project,
            mr_iid=review.mr_iid,
            head_sha=review.head_sha,
            state=review.state,
            milestone=f"M{level}",
        )
        if review.state in TERMINAL:
            snapshot = await self.store.snapshot(review.id)
            if snapshot and snapshot.get("config"):
                config = ProjectConfig.model_validate(snapshot["config"])
            if not review.status_delivered:
                recovered = status(
                    review.decision,
                    config.enforcement,
                    review.state
                    in {"FAILED_INTERNAL", "FAILED_CONTEXT", "CANCELLED", "SUPERSEDED"},
                )
                logger.info(
                    "recovering_status",
                    review_id=review_id,
                    status=recovered,
                    state=review.state,
                )
                await self.forge.set_commit_status(
                    project,
                    review.head_sha,
                    recovered,
                    "ai-review",
                    "Recovered terminal review",
                    "",
                )
                await self.store.mark_status(review.id)
            return review.state
        llm = None
        bundle = None
        decision = "COMMENT_ONLY"
        final = "FAILED_INTERNAL"
        error = None
        try:
            mr = await self.forge.get_merge_request(project, review.mr_iid)
            if mr.head_sha != review.head_sha:
                raise StaleReview()
            if mr.state != "opened" or mr.draft:
                final = "CANCELLED"
                return await self.finish(
                    review,
                    final,
                    decision,
                    config,
                    error="The merge request was closed, merged or made a draft",
                )
            url = mr.repository_url or await self.forge.repository_url(project)
            previous = await self.store.previous(
                review.project_id, review.mr_iid, review.id
            )
            async with self.git.workspace(project, url, review.head_sha) as wt:
                await self.advance(review, "CONTEXT_COLLECTION")
                # Project policy comes from the target base, not the proposed branch.
                base = await self.git.merge_base(wt, mr.target_branch, review.head_sha)
                try:
                    repository_yaml = (
                        await self.git.command(
                            "show",
                            f"{base}:.ai-review.yml",
                            cwd=wt.mirror,
                            limit=100000,
                        )
                    ).decode()
                except Exception:
                    repository_yaml = None
                if repository_yaml and review.execution_config is None:
                    config = load_project(
                        self.settings.config_path, project, repository_yaml
                    )
                from reviewer.context.models import ReviewOverrides
                from reviewer.services.docs.confluence import FakeDocumentService
                from reviewer.services.issues.jira import FakeIssueService
                from reviewer.services.secrets.scanner import FakeSecretScanner

                overrides = (
                    ReviewOverrides.model_validate(review.overrides)
                    if review.overrides
                    else None
                )
                # Resolved once for the whole run: every stage, the verifier and
                # the recheck judge use this map, it is recorded on the budget,
                # and it is announced so the activity feed can name the model
                # behind each event.
                execution = review.execution_config
                if execution is None:
                    models = resolve(self.settings, config, overrides)
                    execution = await self.store.freeze_execution(
                        review.id,
                        dict(
                            config=config.model_dump(mode="json"),
                            models={
                                role: asdict(spec) for role, spec in models.items()
                            },
                        ),
                    )
                config = frozen_config(execution["config"])
                level = min(level, int(config.milestone[1:]))
                models = {
                    role: ResolvedModel(**spec)
                    for role, spec in execution["models"].items()
                }
                selection = assignment(models)
                await self.store.append_event(review.id, "models", selection)
                # The ceiling this run is held to, announced by the run itself:
                # it is the project's, as the merge base configured it, and the
                # spend report is read against it while the review is still going.
                await self.store.append_event(
                    review.id,
                    "budget",
                    {
                        "token_ceiling": config.review.token_ceiling,
                        "timeout_s": config.review.timeout_s,
                    },
                )
                logger.info("models_resolved", review_id=review.id, models=selection)
                bundle, redactor, symbols, secrets, context_provider = await build(
                    review,
                    mr,
                    wt,
                    self.git,
                    self.forge,
                    self.issues if level >= 2 else FakeIssueService(),
                    self.docs if level >= 2 else FakeDocumentService(),
                    self.scanner if level >= 3 else FakeSecretScanner(),
                    config,
                    previous,
                    overrides,
                    selection,
                )
                logger.info(
                    "context_collected",
                    review_id=review.id,
                    files_count=len(bundle.code.files),
                    total_changed_lines=bundle.code.total_changed_lines,
                    secrets_found=len(secrets),
                    degradations=bundle.degradations,
                )
                bundle.jira_base_url = self.settings.jira_base_url
                config_hash = hashlib.sha256(
                    config.model_dump_json().encode()
                ).hexdigest()
                # The injection rule joins every system prompt, so it is part of
                # what a prompt change means for re-review.
                prompt_hash = hashlib.sha256(
                    json.dumps([PROMPTS, INJECTION_RULE], sort_keys=True).encode()
                ).hexdigest()
                only_paths = None
                carried = []
                previous_findings = []
                previous_head = None
                delta = None
                if previous:
                    # Loaded whether or not the re-review is incremental: the
                    # comments already on the merge request are rechecked either
                    # way, and a full re-review is the strongest evidence there is.
                    previous_head = (
                        previous.get("bundle", {}).get("code", {}).get("head_sha")
                    )
                    previous_findings = [
                        Finding.model_validate(f) for f in previous.get("findings", [])
                    ]
                if previous and int(self.settings.milestone[1:]) >= 8:
                    old_bundle = previous.get("bundle", {})
                    old_base = old_bundle.get("code", {}).get("merge_base_sha")
                    distance = 9999
                    try:
                        distance = int(
                            (
                                await self.git.command(
                                    "rev-list",
                                    "--count",
                                    f"{old_base}...{bundle.code.merge_base_sha}",
                                    cwd=wt.mirror,
                                )
                            ).decode()
                        )
                    except Exception:
                        pass
                    if not full_review(previous, config_hash, prompt_hash, distance):
                        delta = await self.git.diff(
                            wt, old_bundle["code"]["head_sha"], review.head_sha
                        )
                        only_paths = {f.path for f in delta}
                        for old in previous_findings:
                            classification, moved = reanchor(old, wt.path)
                            if moved and (
                                old.anchor.file not in only_paths
                                or classification in {"moved", "unchanged"}
                            ):
                                moved.id = str(uuid4())
                                moved.anchor.commit_sha = review.head_sha
                                if moved.severity_final == "BLOCKER":
                                    moved.severity_final = "SUGGESTION"
                                carried.append(moved)
                await self.advance(review, "STATIC_ANALYSIS")
                bundle.static = await asyncio.gather(
                    *(
                        self.static.run(tool, wt.path, redact=redactor.text)
                        for tool in config.static_tools
                        if level >= 3
                    )
                )
                for result in bundle.static:
                    result.stdout_tail = redactor.text(result.stdout_tail)
                if any(s.status in {"errored", "skipped"} for s in bundle.static):
                    bundle.degradations.append("static_unavailable")
                if level < 4:
                    await self.advance(review, "FINALIZATION")
                    await self.advance(review, "DECISION")
                    await self.store.save_snapshot(
                        review.id,
                        {
                            "bundle": bundle.model_dump(mode="json"),
                            "findings": [],
                            "partial": True,
                            "config_hash": config_hash,
                            "prompt_hash": prompt_hash,
                        },
                    )
                    return await self.finish(
                        review, "PUBLISHED", "COMMENT_ONLY", config, True
                    )
                # A run recovered from before deep mode was removed may hold
                # results of stages that no longer exist; only this one counts.
                results = {
                    name: result
                    for name, result in (await self.store.stages(review.id)).items()
                    if name == "defect_review"
                }
                from sqlalchemy import func, select

                from reviewer.store.models import LLMCall

                async with self.store.sessions() as session:
                    bundle.budget.tokens_used = int(
                        await session.scalar(
                            select(
                                func.coalesce(
                                    func.sum(LLMCall.tokens_in + LLMCall.tokens_out), 0
                                )
                            ).where(
                                LLMCall.review_id == review.id,
                                # Questions asked about the review are paid for
                                # from the chat budget, never the review's own.
                                LLMCall.stage != "chat",
                            )
                        )
                    )
                tracker = BudgetTracker(
                    bundle.budget,
                    config.final_stage_token_reserve,
                    min(config.publication_reserve_s, config.review.timeout_s / 10),
                )
                try:
                    llm = (
                        self.llm_factory(bundle, redactor)
                        if self.llm_factory
                        else GatewayClient(
                            self.settings,
                            Audit(
                                self.store,
                                blob_store(self.settings),
                            ),
                            tracker,
                            redactor,
                            models=models,
                            semaphore=self.gateway_slots,
                        )
                    )
                except ValueError:
                    llm = None
                    bundle.degradations.append("gateway_unconfigured")

                async def stage(name, observer=None):
                    results[name] = await execute(
                        name,
                        bundle,
                        llm,
                        config,
                        context_provider,
                        only_paths,
                        self.store,
                        redactor,
                        results.get(name),
                        observer,
                    )
                    from reviewer.orchestrator.stages import StageResult

                    results[name] = StageResult.model_validate(
                        redactor.object(results[name].model_dump(mode="json"))
                    )
                    await self.store.save_stage(review.id, results[name])
                    return results[name]

                categories = {
                    c
                    for tool in config.static_tools
                    if any(
                        s.name == tool.name and s.status in {"passed", "failed"}
                        for s in bundle.static
                    )
                    for c in tool.categories
                }
                touched = {file.path for file in bundle.code.files}
                verifiers = asyncio.Semaphore(config.verification_concurrency)
                commits = None
                if level >= 5:
                    try:
                        commits = await change_commits(self.git, wt, bundle)
                    except Exception:
                        # Unknown attribution is non-blocking: validate()
                        # retries per finding and falls back to not-introduced.
                        commits = None

                def unpublishable(f):
                    # Verification is the costliest call per finding: spend it
                    # only where the result can reach the merge request.
                    return f.category in categories or (
                        not f.anchor.in_diff and f.anchor.file not in touched
                    )

                def skipped(f, beyond):
                    """Why `f` has no verdict, when no verifier was meant to run."""
                    if f.category in categories:
                        return "static_tool"
                    if not f.anchor.in_diff and f.anchor.file not in touched:
                        return "outside_change"
                    if beyond:
                        return "existing_limit"
                    if f.validation and not f.validation.evidence_valid:
                        return "evidence_invalid"
                    if not needs_verification(f):
                        return "not_required"
                    return None

                async def verify(values):
                    if level >= 6 and values:
                        await verify_findings(
                            values,
                            bundle,
                            llm,
                            redactor,
                            context_provider,
                            config.verification_concurrency,
                            config=config,
                            slots=verifiers,
                        )

                def make(proposed, stage):
                    return Finding(
                        **proposed.model_dump(),
                        id=str(uuid4()),
                        fingerprint=fingerprint(
                            project,
                            proposed.anchor.file,
                            proposed.category,
                            proposed.claim,
                            proposed.anchor.symbol,
                        ),
                        stage=stage,
                        provenance=Provenance(
                            agent=stage,
                            prompt_version=PROMPTS[stage][0],
                            model=getattr(llm, "models", {}).get(stage, "fake"),
                            run_id=review.id,
                            context_bundle_hash=bundle.content_hash(),
                        ),
                    )

                async def prepare(proposals, stage="defect_review"):
                    """Validate, deduplicate and verify one file's proposals.

                    Pre-existing findings wait for `settle`: which two of them
                    publication shows is decided across every file.
                    """
                    values, discarded = [], []
                    for proposed in proposals:
                        f = make(proposed, stage)
                        await validate(
                            f, bundle, symbols, self.git, wt, commits=commits
                        )
                        (discarded if f.status == "discarded" else values).append(f)
                    values = deduplicate(values, config.review.merge_distance)
                    await verify(
                        [
                            f
                            for f in values
                            if not unpublishable(f)
                            and f.anchor.introduced_by_this_change
                        ]
                    )
                    return values, discarded

                async def settle(values):
                    existing = sorted(
                        (
                            f
                            for f in values
                            if not unpublishable(f)
                            and not f.anchor.introduced_by_this_change
                        ),
                        key=lambda f: (
                            {"high": 0, "medium": 1, "low": 2}[f.confidence],
                            -len(f.evidence),
                        ),
                    )
                    # Pre-existing code is reported as at most two FYIs. A
                    # verdict a build's own output established is not re-judged.
                    await verify(
                        [
                            f
                            for f in existing[:EXISTING_FINDINGS]
                            if f.verification is None
                        ]
                    )
                    beyond = {id(f) for f in existing[EXISTING_FINDINGS:]}
                    for f in values:
                        if id(f) in beyond:
                            f.status = "suppressed"
                        if f.verification is None:
                            f.unverified_reason = skipped(f, id(f) in beyond)
                        normalize(f, touched, categories)
                    if any(
                        f.evidence_scope != "local"
                        and not f.verified
                        and f.status == "suppressed"
                        and id(f) not in beyond
                        and not unpublishable(f)
                        for f in values
                    ):
                        bundle.degradations.append("unverified_scope_claims")
                    return values

                async def established(proposals):
                    """Findings a sandboxed command's own diagnostics establish.

                    Validated like any proposal, then confirmed by that output
                    rather than by a model: deterministic evidence from the run
                    outranks a judge.
                    """
                    values, discarded = [], []
                    for proposed in proposals:
                        f = make(proposed, "build_agent")
                        await validate(
                            f, bundle, symbols, self.git, wt, commits=commits
                        )
                        if f.status == "discarded":
                            discarded.append(f)
                            continue
                        f.verification = VerificationResult(
                            counterargument="",
                            reasoning="Reported by the sandboxed build's own "
                            "diagnostic output; no model judged it.",
                            verdict="confirmed",
                        )
                        values.append(f)
                    return (
                        deduplicate(values, config.review.merge_distance),
                        discarded,
                    )

                findings = secrets
                if level >= 5:
                    await self.advance(review, "DEFECT_REVIEW")
                    # The build needs nothing from the Defect Review, so it runs
                    # alongside it; its results join before findings settle.
                    building = asyncio.create_task(
                        BuildAgent(
                            self.settings, config, redactor, context_provider
                        ).run(bundle, wt, llm)
                    )
                    try:
                        # Validation and verification of a file start as soon as
                        # its units are done; the states below mark the remainder.
                        async with asyncio.TaskGroup() as group:
                            stream = FileStream(
                                group,
                                prepare,
                                lambda proposal: ProposedFinding.model_validate(
                                    redactor.object(proposal.model_dump(mode="json"))
                                ),
                            )
                            result = await stage("defect_review", stream)
                            await self.advance(review, "EVIDENCE_VALIDATION")
                            await self.advance(review, "FINDING_VERIFICATION")
                            fresh, discarded = await stream.finish(result.findings)
                        built = await building
                    finally:
                        if not building.done():
                            building.cancel()
                            await asyncio.gather(building, return_exceptions=True)
                    bundle.static.extend(built.static)
                    confirmed, rejected = await established(built.diagnostics)
                    proposed, dropped = await prepare(
                        [
                            ProposedFinding.model_validate(
                                redactor.object(p.model_dump(mode="json"))
                            )
                            for p in built.proposals
                        ],
                        "build_agent",
                    )
                    fresh += confirmed + proposed
                    discarded += rejected + dropped
                    if discarded:
                        await self.store.save_findings(review, discarded)
                    fresh = await settle(fresh)
                    # A cheap model may lower, never raise, what code decided.
                    await SeverityTriageAgent().triage(
                        fresh, bundle, llm, config, context_provider
                    )
                    if previous_findings:
                        if delta is None and previous_head:
                            try:
                                delta = await self.git.diff(
                                    wt, previous_head, review.head_sha
                                )
                            except Exception:
                                # A rewritten history leaves no path to follow;
                                # snippets that survived still reanchor by hash.
                                delta = None
                        adopt_identities(fresh, previous_findings, wt.path, delta)
                    # Published identities come before fresh claims so a merge
                    # keeps the fingerprint the open thread already carries.
                    findings = deduplicate(
                        secrets + carried + fresh,
                        config.review.merge_distance,
                    )
                for r in results.values():
                    if r.failed:
                        bundle.degradations.append("failed_stage:" + r.stage)
                    if r.partial:
                        bundle.degradations.append("partial:" + r.stage)
                from reviewer.telemetry import REVIEWS

                REVIEWS.labels(state="analysis_complete").inc()
                partial = (
                    any(r.partial or r.failed for r in results.values())
                    or llm is None
                    or "prompt_requirements_truncated" in bundle.degradations
                    or "budget_exhausted" in bundle.degradations
                    or "unverified_scope_claims" in bundle.degradations
                    or level < 7
                )
                if partial:
                    bundle.degradations.append("partial")
                await self.advance(review, "FINALIZATION")
                decision = decide(
                    SimpleNamespace(partial=partial, complete=True),
                    findings,
                    bundle.static,
                    config,
                )
                logger.info(
                    "decision_made",
                    review_id=review.id,
                    decision=str(decision),
                    findings_count=len(findings),
                    partial=partial,
                    early=False,
                )
                for f in findings:
                    f.provenance.context_bundle_hash = bundle.content_hash()
                data = {
                    "bundle": bundle.model_dump(mode="json"),
                    "findings": [f.model_dump(mode="json") for f in findings],
                    "config_hash": config_hash,
                    "config": config.model_dump(mode="json"),
                    "prompt_hash": prompt_hash,
                    "issue_updated": getattr(self.issues, "raw", {})
                    .get(bundle.issue.key if bundle.issue else "", {})
                    .get("fields", {})
                    .get("updated"),
                    "partial": partial,
                    "full_rereview_merge_base_delta": config.review.full_rereview_merge_base_delta,
                }
                await self.store.save_snapshot(review.id, data)
                await self.store.save_findings(review, findings)
                if (
                    await self.forge.get_merge_request(project, review.mr_iid)
                ).head_sha != review.head_sha:
                    raise StaleReview()
                # Persist decision before publication. Findings and stage outputs
                # remain recoverable if the worker is interrupted during posting.
                await self.advance(review, "DECISION")
                if level >= 7:
                    # Answer the comments already on the merge request before
                    # adding more: the author's fixes are what this push is about.
                    recheck_report = await self.recheck(
                        review,
                        project,
                        config,
                        wt,
                        previous_findings,
                        previous_head,
                        only_paths,
                        findings + carried,
                        not partial,
                        llm,
                        redactor,
                        overrides.report_mode if overrides else None,
                    )
                    report = await Publisher(self.forge, self.store).publish(
                        bundle,
                        findings,
                        decision,
                        config,
                        list(results.values()),
                        overrides.report_mode if overrides else None,
                    )
                    # A drafted report is never posted, so the snapshot is the
                    # only place an operator can read it back from.
                    if report is not None or recheck_report is not None:
                        await self.store.save_snapshot(
                            review.id,
                            data | {"report": report, "recheck": recheck_report},
                        )
                await self.store.save_findings(review, findings)
                final = "PUBLISHED"
                return await self.finish(review, final, decision, config, partial)
        except StaleReview:
            final = "SUPERSEDED"
            logger.info("review_stale", review_id=review.id, head_sha=review.head_sha)
            current = await self.forge.get_merge_request(project, review.mr_iid)
            await self.store.transition(
                review.id,
                final,
                **(
                    {
                        "error": "The merge request received new commits during the review"
                    }
                    if current.head_sha != review.head_sha
                    else {}
                ),
            )
            if current.head_sha != review.head_sha:
                logger.info(
                    "review_superseded_by_new_head",
                    review_id=review.id,
                    old_sha=review.head_sha,
                    new_sha=current.head_sha,
                )
                await self.store.accept(
                    project,
                    review.mr_iid,
                    current.head_sha,
                    f"supersede:{review.id}:{current.head_sha}",
                )
        except (Exception,) as exc:
            persisted = await self.store.get(review.id)
            if persisted.state in TERMINAL:
                raise
            from reviewer.services.git.service import GitError
            from reviewer.services.secrets.scanner import ScanError

            final = (
                "FAILED_CONTEXT"
                if isinstance(exc, (GitError, ScanError, FileNotFoundError))
                else "FAILED_INTERNAL"
            )
            if final == "FAILED_CONTEXT" and config.enforcement != "silent":
                try:
                    await self.forge.post_note(
                        project,
                        review.mr_iid,
                        "AI review could not collect repository context. No review findings were published; the check passes.",
                    )
                except Exception:
                    pass
            logger.error(
                "review_failed",
                review_id=review.id,
                error_type=type(exc).__name__,
                final_state=final,
                error=str(exc),
            )
            # Named by kind and stage, never by exception text.
            error = failure_reason(exc, persisted.state)
        finally:
            if llm and hasattr(llm, "close"):
                await llm.close()
        return await self.finish(
            review, final, "COMMENT_ONLY", config, True, error=error
        )

    @activity("phase", "Recheck comments")
    async def recheck(
        self,
        review,
        project,
        config,
        wt,
        previous_findings,
        previous_head,
        only_paths,
        findings,
        complete,
        llm,
        redactor,
        report_mode,
    ):
        """Answer each open reviewer thread with what this head did to its claim.

        Returns None when there was nothing to recheck. Writes are governed
        exactly as the report's are: silent enforcement and a "none" report mode
        publish nothing, and a drafted run queues the replies as GitLab draft
        notes instead of posting them into the threads.
        """
        from datetime import UTC, datetime

        from reviewer.publish.recheck import collect, evaluate
        from reviewer.publish.recheck import publish as publish_recheck

        if not previous_findings or not config.review.recheck:
            return None
        if config.enforcement == "silent" or report_mode == "none":
            return None
        draft = report_mode == "draft"
        threads = await collect(
            self.forge,
            self.store,
            project,
            review.mr_iid,
            previous_findings,
            review.head_sha,
            draft,
        )
        if not threads:
            return None
        live = {
            f.fingerprint
            for f in findings
            if f.status not in {"discarded", "suppressed", "resolved"}
        }
        # A finished re-review that no longer reports a claim is stronger
        # evidence than any single-claim judgement. A partial or early-terminated
        # one is no evidence at all, and an incremental run speaks only for the
        # files it re-ran.
        absent = frozenset(
            finding.fingerprint
            for finding, *_ in threads
            if complete
            and finding.fingerprint not in live
            and (only_paths is None or finding.anchor.file in only_paths)
        )
        verdicts = await evaluate(
            threads,
            root=wt.path,
            touched=only_paths,
            absent=absent,
            reported=live,
            git=self.git,
            wt=wt,
            previous_head=previous_head,
            llm=llm,
            redactor=redactor,
            review_id=review.id,
            limit=config.review.recheck_max_judgements,
        )
        if (
            not draft
            and (await self.forge.get_merge_request(project, review.mr_iid)).head_sha
            != review.head_sha
        ):
            raise StaleReview()
        posted = await publish_recheck(
            self.forge,
            self.store,
            project,
            review.mr_iid,
            review.project_id,
            verdicts,
            review.head_sha,
            draft,
        )
        logger.info(
            "recheck_complete",
            review_id=review.id,
            project_id=project,
            iid=review.mr_iid,
            threads=len(verdicts),
            judged=sum(1 for v in verdicts if v.judged),
        )
        return {
            "mode": "draft" if draft else "applied",
            "at": datetime.now(UTC).isoformat(),
            "head_sha": review.head_sha,
            "previous_head_sha": previous_head,
            "verdicts": [v.model_dump(mode="json") for v in verdicts],
            "posted": posted,
        }

    async def recheck_now(self, project_id, iid, review_id=None):
        from datetime import UTC, datetime

        token = sink.set((self.store, review_id)) if review_id is not None else None
        try:
            work = self._recheck_now(project_id, iid, review_id)
            # A command names no review and points activity at the one it
            # answers; its own task keeps that from outliving the recheck.
            result = await (
                work if review_id is not None else asyncio.create_task(work)
            )
        except Exception:
            if review_id is not None:
                stored = await self.store.snapshot(review_id) or {}
                await self.store.save_snapshot(
                    review_id,
                    stored
                    | {
                        "recheck": {
                            "at": datetime.now(UTC).isoformat(),
                            "reason": "failed",
                            "verdicts": [],
                            "posted": [],
                        }
                    },
                )
            raise
        finally:
            if token is not None:
                sink.reset(token)
        if review_id is not None:
            report = {
                "at": datetime.now(UTC).isoformat(),
                "verdicts": [],
                "posted": [],
            } | (result or {"reason": "no_open_threads"})
            stored = await self.store.snapshot(review_id) or {}
            await self.store.save_snapshot(review_id, stored | {"recheck": report})
            return report
        return result

    @activity("phase", "Standalone recheck")
    async def _recheck_now(self, project_id, iid, review_id=None):
        """Recheck a merge request's open comments at its current head.

        The `/ai recheck` path. No review is admitted, no stage runs and no
        report is published: the threads already on the merge request are
        answered against whatever the branch now points at. A push mid-recheck
        is harmless — the reply carries the head it judged, and the next run
        answers the newer one.
        """
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from reviewer.context.models import Budget
        from reviewer.context.redaction import Redactor

        config = load_project(self.settings.config_path, project_id)
        if not config.review.recheck or config.enforcement == "silent":
            return {"rechecked": False, "reason": "disabled"}
        internal = await self.store.internal_project(project_id)
        if internal is None:
            return {"rechecked": False, "reason": "project_not_onboarded"}
        previous_id, previous = await self.store.latest_published(
            internal,
            iid,
            getattr(self, "owner_user_id", None),
            getattr(self, "principal_id", None),
        )
        if not previous:
            return {"rechecked": False, "reason": "no_published_review"}
        if review_id is None and sink.get() is None:
            # An `/ai recheck` names no review; its progress belongs to the
            # one whose threads it answers, where its result is recorded too.
            sink.set((self.store, previous_id))
        source_review = await self.store.get(previous_id)
        from reviewer.context.models import ReviewOverrides

        source_overrides = (
            ReviewOverrides.model_validate(source_review.overrides)
            if source_review and source_review.overrides
            else None
        )
        if source_review and source_review.execution_config:
            config = frozen_config(source_review.execution_config["config"])
        if config.enforcement == "silent" or (
            source_overrides and source_overrides.report_mode == "none"
        ):
            return {"rechecked": False, "reason": "disabled"}
        mr = await self.forge.get_merge_request(project_id, iid)
        if mr.state != "opened" or mr.draft:
            return {"rechecked": False, "reason": "merge_request_not_open"}
        previous_findings = [
            Finding.model_validate(f) for f in previous.get("findings", [])
        ]
        if not previous_findings:
            return {"rechecked": False, "reason": "no_published_findings"}
        url = mr.repository_url or await self.forge.repository_url(project_id)
        # No secret scan runs here, so only the pattern-based redaction applies.
        redactor = Redactor()
        # A standalone recheck carries no operator overrides: it runs on the
        # project's own judge, exactly as the recheck inside a review does.
        recheck_models = resolve(self.settings, config)
        logger.info(
            "recheck_now_start",
            project_id=project_id,
            iid=iid,
            head_sha=mr.head_sha,
            previous_review_id=previous_id,
        )
        async with self.git.workspace(project_id, url, mr.head_sha) as wt:
            llm = None
            try:
                try:
                    llm = (
                        self.llm_factory(None, redactor)
                        if self.llm_factory
                        else GatewayClient(
                            self.settings,
                            Audit(self.store, blob_store(self.settings)),
                            BudgetTracker(
                                Budget(
                                    token_ceiling=config.review.token_ceiling,
                                    deadline_at=datetime.now(UTC)
                                    + timedelta(seconds=config.review.timeout_s),
                                    model_tier=assignment(recheck_models),
                                ),
                                deadline_reserve_s=min(
                                    config.publication_reserve_s,
                                    config.review.timeout_s / 10,
                                ),
                            ),
                            redactor,
                            models=recheck_models,
                            semaphore=self.gateway_slots,
                        )
                    )
                except ValueError:
                    llm = None
                report = await self.recheck(
                    SimpleNamespace(
                        id=review_id or previous_id,
                        mr_iid=iid,
                        head_sha=mr.head_sha,
                        project_id=internal,
                    ),
                    project_id,
                    config,
                    wt,
                    previous_findings,
                    previous.get("bundle", {}).get("code", {}).get("head_sha"),
                    None,
                    [],
                    False,
                    llm,
                    redactor,
                    source_overrides.report_mode if source_overrides else None,
                )
                if report is not None and review_id is None:
                    # The recheck runs outside any review of its own, so its
                    # answers are recorded on the review that published the
                    # threads; that is where an operator reads them back.
                    stored = await self.store.snapshot(previous_id)
                    if stored is not None:
                        await self.store.save_snapshot(
                            previous_id, stored | {"recheck": report}
                        )
                return report
            finally:
                if llm and hasattr(llm, "close"):
                    await llm.close()

    async def advance(self, review, state):
        current = await self.store.get(review.id)
        if current.state in TERMINAL:
            raise StaleReview()
        if ORDER.index(current.state) < ORDER.index(state):
            logger.info(
                "state_transition",
                review_id=review.id,
                from_state=current.state,
                to_state=state,
            )
            await self.store.transition(review.id, state)

    async def finish(self, review, state, decision, config, partial=False, error=None):
        logger.info(
            "pipeline_finish",
            review_id=review.id,
            state=state,
            decision=str(decision),
            partial=partial,
        )
        fields = dict(decision=str(decision), partial=partial)
        if error:
            fields["error"] = error
        result = await self.store.transition(review.id, state, **fields)
        failed = state in {
            "FAILED_INTERNAL",
            "FAILED_CONTEXT",
            "CANCELLED",
            "SUPERSEDED",
        }
        code = status(decision, config.enforcement, failed)
        project = await self.store.project_number(review)
        await self.forge.set_commit_status(
            project,
            review.head_sha,
            code,
            "ai-review",
            str(decision) if not failed else "Review unavailable; passing by policy",
            "",
        )
        await self.store.mark_status(review.id)
        return result.state
