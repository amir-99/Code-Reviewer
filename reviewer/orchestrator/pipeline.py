import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import structlog

from reviewer.agents.base import PROMPTS
from reviewer.agents.verification import verify
from reviewer.config.loader import load_project
from reviewer.context.builder import build
from reviewer.decision.engine import decide, status
from reviewer.findings.dedup import deduplicate, fingerprint
from reviewer.findings.models import Finding, Provenance, VerificationResult
from reviewer.findings.policy import needs_verification, normalize
from reviewer.findings.validator import validate
from reviewer.orchestrator.budget import BudgetTracker
from reviewer.orchestrator.stages import execute
from reviewer.orchestrator.states import TERMINAL
from reviewer.publish.publisher import Publisher, existing
from reviewer.publish.rereview import full_review, reanchor, resolve_fixed
from reviewer.services.forge.gitlab import StaleReview
from reviewer.services.llm.client import GatewayClient
from reviewer.store.audit import Audit, blob_store
from reviewer.telemetry.activity import traced_review

logger = structlog.get_logger()


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

    @traced_review
    async def run(self, review_id):
        review = await self.store.get(review_id)
        if not review:
            logger.warning("review_not_found", review_id=review_id)
            return
        project = await self.store.project_number(review)
        config = load_project(self.settings.config_path, project)
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
                from reviewer.config.schema import ProjectConfig

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
        try:
            mr = await self.forge.get_merge_request(project, review.mr_iid)
            if mr.head_sha != review.head_sha:
                raise StaleReview()
            if mr.state != "opened" or mr.draft:
                final = "CANCELLED"
                return await self.finish(review, final, decision, config)
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
                if repository_yaml:
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
                prompt_hash = hashlib.sha256(
                    json.dumps(PROMPTS, sort_keys=True).encode()
                ).hexdigest()
                only_paths = None
                carried = []
                previous_findings = []
                system_paths = None
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
                        old_files = {f["path"] for f in old_bundle["code"]["files"]}
                        new_files = {f.path for f in bundle.code.files}
                        system_paths = None if old_files != new_files else set()
                        previous_findings = [
                            Finding.model_validate(f)
                            for f in previous.get("findings", [])
                        ]
                        for old in previous_findings:
                            classification, moved = reanchor(old, wt.path)
                            if moved and (
                                old.anchor.file not in only_paths
                                or classification in {"moved", "unchanged"}
                            ):
                                moved.id = str(uuid4())
                                moved.anchor.commit_sha = review.head_sha
                                carried.append(moved)
                await self.advance(review, "STATIC_ANALYSIS")
                bundle.static = await asyncio.gather(
                    *(
                        self.static.run(tool, wt.path)
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
                results = await self.store.stages(review.id)
                from sqlalchemy import func, select

                from reviewer.store.models import LLMCall

                async with self.store.sessions() as session:
                    bundle.budget.tokens_used = int(
                        await session.scalar(
                            select(
                                func.coalesce(
                                    func.sum(LLMCall.tokens_in + LLMCall.tokens_out), 0
                                )
                            ).where(LLMCall.review_id == review.id)
                        )
                    )
                tracker = BudgetTracker(bundle.budget)
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
                        )
                    )
                except ValueError:
                    llm = None
                    bundle.degradations.append("gateway_unconfigured")

                async def stage(name):
                    if name not in results:
                        results[name] = await execute(
                            name,
                            bundle,
                            llm,
                            config,
                            context_provider,
                            system_paths if name == "system_context" else only_paths,
                        )
                        from reviewer.orchestrator.stages import StageResult

                        results[name] = StageResult.model_validate(
                            redactor.object(results[name].model_dump(mode="json"))
                        )
                        await self.store.save_stage(review.id, results[name])
                    return results[name]

                async def process(raw):
                    values = []
                    for name, proposed in raw:
                        f = Finding(
                            **proposed.model_dump(),
                            id=str(uuid4()),
                            fingerprint=fingerprint(
                                project,
                                proposed.anchor.file,
                                proposed.category,
                                proposed.claim,
                                proposed.anchor.symbol,
                            ),
                            stage=name,
                            provenance=Provenance(
                                agent=name,
                                prompt_version=PROMPTS[name][0],
                                model=(
                                    getattr(llm, "models", {}).get(
                                        "fast"
                                        if name in {"complexity", "line_review"}
                                        else "strong",
                                        "fake",
                                    )
                                ),
                                run_id=review.id,
                                context_bundle_hash=bundle.content_hash(),
                            ),
                        )
                        await validate(f, bundle, symbols, self.git, wt)
                        if f.status != "discarded":
                            values.append(f)
                        else:
                            await self.store.save_findings(review, [f])
                    values = deduplicate(values, config.review.merge_distance)
                    for f in values:
                        if (
                            needs_verification(f)
                            and level >= 6
                            and (not f.validation or f.validation.evidence_valid)
                        ):
                            try:
                                from reviewer.findings.models import ContextRequest

                                # Scan all cited files before an independent verifier sees them.
                                await context_provider(
                                    [
                                        ContextRequest(
                                            kind="file",
                                            target=path,
                                            reason="verification evidence",
                                        )
                                        for path in dict.fromkeys(
                                            [f.anchor.file]
                                            + [e.file for e in f.evidence]
                                        )
                                    ]
                                )
                                await verify(f, bundle, llm, redactor)
                            except Exception as verification_error:
                                from reviewer.orchestrator.budget import BudgetExhausted

                                if isinstance(verification_error, BudgetExhausted):
                                    bundle.degradations.append("budget_exhausted")
                                f.verification = VerificationResult(
                                    verdict="uncertain",
                                    counterargument="Verification unavailable",
                                    reasoning="Cannot confirm within budget",
                                )
                        categories = {
                            c
                            for tool in config.static_tools
                            if any(
                                s.name == tool.name and s.status in {"passed", "failed"}
                                for s in bundle.static
                            )
                            for c in tool.categories
                        }
                        normalize(
                            f, {file.path for file in bundle.code.files}, categories
                        )
                    return values

                early = bool(secrets)
                findings = secrets
                raw = []
                for name, state in [
                    ("purpose", "PURPOSE_REVIEW"),
                    ("design", "DESIGN_REVIEW"),
                ]:
                    if early:
                        break
                    await self.advance(review, state)
                    result = await stage(name)
                    raw.extend((name, f) for f in result.findings)
                    checked = await process([(name, f) for f in result.findings])
                    blocker = next(
                        (
                            f
                            for f in checked
                            if f.severity_final == "BLOCKER"
                            and f.verified
                            and f.status != "discarded"
                        ),
                        None,
                    )
                    if blocker:
                        findings = [blocker]
                        early = True
                        logger.info(
                            "gate_terminated_early",
                            review_id=review.id,
                            stage=name,
                            fingerprint=blocker.fingerprint,
                            severity=blocker.severity_final,
                        )
                        break
                if not early and level >= 5:
                    await self.advance(review, "ANALYSIS_FAN_OUT")
                    names = (
                        ["tests_"]
                        if "triage_mode" in bundle.degradations
                        else ["correctness", "complexity", "tests_", "line_review"]
                    )
                    fan = await asyncio.gather(
                        *(stage(n) for n in names), return_exceptions=True
                    )
                    raw.extend(
                        (r.stage, f)
                        for r in fan
                        if not isinstance(r, Exception)
                        for f in r.findings
                    )
                    await self.advance(review, "SYSTEM_CONTEXT_REVIEW")
                    # Include aggregated claims as data for the system stage.
                    bundle.aggregated_findings = [
                        {"stage": n, "claim": f.claim, "anchor": f.anchor.model_dump()}
                        for n, f in raw
                    ][:100]
                    result = await stage("system_context")
                    raw.extend(("system_context", f) for f in result.findings)
                    await self.advance(review, "EVIDENCE_VALIDATION")
                    await self.advance(review, "FINDING_VERIFICATION")
                    findings = deduplicate(
                        await process(raw) + carried, config.review.merge_distance
                    )
                if not early and level < 5:
                    findings = await process(raw)
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
                    or "whole_change_summary_truncated" in bundle.degradations
                    or level < 7
                ) and not secrets
                if partial:
                    bundle.degradations.append("partial")
                if not early:
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
                    early=early,
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
                if not early:
                    await self.advance(review, "DECISION")
                if level >= 7:
                    if previous_findings and config.enforcement != "silent":
                        discussions, _ = await existing(
                            self.forge, project, review.mr_iid
                        )
                        fixed = await resolve_fixed(
                            self.forge,
                            project,
                            review.mr_iid,
                            previous_findings,
                            findings,
                            discussions,
                            not partial,
                            only_paths or set(),
                        )
                        for f in fixed:
                            await self.store.outcome(
                                review.project_id,
                                review.mr_iid,
                                f.fingerprint,
                                "actioned",
                                "No longer present after complete re-review",
                                "system",
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
                    if report is not None:
                        await self.store.save_snapshot(
                            review.id, data | {"report": report}
                        )
                await self.store.save_findings(review, findings)
                final = "TERMINATED_EARLY" if early else "PUBLISHED"
                return await self.finish(review, final, decision, config, partial)
        except StaleReview:
            final = "SUPERSEDED"
            logger.info("review_stale", review_id=review.id, head_sha=review.head_sha)
            current = await self.forge.get_merge_request(project, review.mr_iid)
            await self.store.transition(review.id, final)
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
        finally:
            if llm and hasattr(llm, "close"):
                await llm.close()
        return await self.finish(review, final, "COMMENT_ONLY", config, True)

    async def advance(self, review, state):
        current = await self.store.get(review.id)
        if current.state in TERMINAL:
            raise StaleReview()
        order = [
            "INIT",
            "CONTEXT_COLLECTION",
            "STATIC_ANALYSIS",
            "PURPOSE_REVIEW",
            "DESIGN_REVIEW",
            "ANALYSIS_FAN_OUT",
            "SYSTEM_CONTEXT_REVIEW",
            "EVIDENCE_VALIDATION",
            "FINDING_VERIFICATION",
            "FINALIZATION",
            "DECISION",
        ]
        if order.index(current.state) < order.index(state):
            logger.info(
                "state_transition",
                review_id=review.id,
                from_state=current.state,
                to_state=state,
            )
            await self.store.transition(review.id, state)

    async def finish(self, review, state, decision, config, partial=False):
        logger.info(
            "pipeline_finish",
            review_id=review.id,
            state=state,
            decision=str(decision),
            partial=partial,
        )
        result = await self.store.transition(
            review.id, state, decision=str(decision), partial=partial
        )
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
