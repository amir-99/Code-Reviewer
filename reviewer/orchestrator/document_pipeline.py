"""The state machine for a review of a Confluence page.

INIT → CONTEXT_COLLECTION → DOCUMENT_REVIEW → EVIDENCE_VALIDATION →
FINDING_VERIFICATION → FINALIZATION → DECISION → PUBLISHED. Code owns the
order, coverage, anchoring, severity and publication; the models only propose.
There is no merge request, so no commit status is ever set: the decision is a
label on the record, and enforcement decides only whether Confluence may be
written.
"""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog

from reviewer.agents.document_review import DocumentSources, prompt_version, review_unit
from reviewer.agents.document_verification import verify
from reviewer.config.loader import load_project
from reviewer.config.models import ResolvedModel, assignment, resolve
from reviewer.config.schema import ProjectConfig
from reviewer.context.documents import DocumentCorpus
from reviewer.context.models import Budget, ReviewOverrides
from reviewer.context.redaction import Redactor
from reviewer.findings import document as policy
from reviewer.findings.document import DocumentFinding
from reviewer.findings.models import Provenance, Severity
from reviewer.orchestrator.budget import BudgetExhausted, BudgetTracker
from reviewer.orchestrator.stages import StageResult
from reviewer.orchestrator.states import ORDER, TERMINAL
from reviewer.publish.document_publisher import DocumentPublisher
from reviewer.services.forge.gitlab import StaleReview
from reviewer.services.llm.client import GatewayClient, StageFailed
from reviewer.store.audit import Audit, blob_store
from reviewer.telemetry.activity import record, traced_review

logger = structlog.get_logger()


class DocumentPipeline:
    def __init__(self, store, docs, settings, scanner, llm_factory=None):
        self.store, self.docs, self.settings, self.scanner = (
            store,
            docs,
            settings,
            scanner,
        )
        self.llm_factory = llm_factory
        self.gateway_slots = asyncio.Semaphore(settings.gateway_concurrency)
        self.owner_user_id = self.principal_id = None

    @traced_review
    async def run(self, review_id):
        review = await self.store.get(review_id)
        if review is None or review.kind != "document":
            logger.warning("document_review_not_found", review_id=review_id)
            return
        if review.state in TERMINAL:
            return review.state
        subject = dict(review.subject or {})
        config = (
            ProjectConfig.model_validate(review.execution_config["config"])
            if review.execution_config
            else load_project(self.settings.config_path, 0)
        )
        overrides = ReviewOverrides.model_validate(review.overrides or {})
        logger.info(
            "document_pipeline_start",
            review_id=review_id,
            page_id=subject.get("page_id"),
            version=subject.get("version"),
            state=review.state,
        )
        llm = None
        final, partial, decision = "FAILED_INTERNAL", False, "COMMENT_ONLY"
        degradations = []
        try:
            execution = review.execution_config
            if execution is None:
                models = resolve(self.settings, config, overrides)
                execution = await self.store.freeze_execution(
                    review.id,
                    dict(
                        config=config.model_dump(mode="json"),
                        models={role: asdict(spec) for role, spec in models.items()},
                    ),
                )
            config = ProjectConfig.model_validate(execution["config"])
            models = {
                role: ResolvedModel(**spec)
                for role, spec in execution["models"].items()
            }
            await self.store.append_event(review.id, "models", assignment(models))
            await self.store.append_event(
                review.id,
                "budget",
                {
                    "token_ceiling": config.document_review.token_ceiling,
                    "timeout_s": config.document_review.timeout_s,
                },
            )
            await self.advance(review, "CONTEXT_COLLECTION")
            redactor = Redactor()
            corpus = DocumentCorpus(subject, redactor)
            try:
                await corpus.collect(self.docs, overrides, config, self.scanner)
            except LookupError as exc:
                final = "FAILED_CONTEXT"
                return await self.finish(
                    review, final, decision, partial=True, error=str(exc)
                )
            degradations = corpus.degradations
            snapshot = {
                "config": config.model_dump(mode="json"),
                "bundle": corpus.record()
                | {
                    "budget": {
                        "token_ceiling": config.document_review.token_ceiling,
                        "model_tier": assignment(models),
                    },
                    "instruction": overrides.instruction,
                    "check_space": overrides.check_space,
                },
            }
            await self.store.save_snapshot(review.id, snapshot)
            budget = Budget(
                token_ceiling=config.document_review.token_ceiling,
                deadline_at=datetime.now(UTC)
                + timedelta(seconds=config.document_review.timeout_s),
                model_tier=assignment(models),
                tokens_used=await self._spent(review.id),
            )
            tracker = BudgetTracker(
                budget,
                0,
                min(
                    config.publication_reserve_s, config.document_review.timeout_s / 10
                ),
            )
            try:
                llm = (
                    self.llm_factory(corpus, redactor)
                    if self.llm_factory
                    else GatewayClient(
                        self.settings,
                        Audit(self.store, blob_store(self.settings)),
                        tracker,
                        redactor,
                        models=models,
                        semaphore=self.gateway_slots,
                    )
                )
            except ValueError:
                llm = None
                degradations.append("gateway_unconfigured")
            # -- review --------------------------------------------------------
            await self.advance(review, "DOCUMENT_REVIEW")
            proposals, result = [], StageResult(stage="document_review")
            if llm is None or models.get("document_review") is None:
                result.failed, partial = True, True
                result.notes.append("document_review model unavailable")
            else:
                proposals, result = await self._review(
                    review, corpus, overrides, config, llm, models["document_review"]
                )
                partial = partial or result.partial or result.failed
            await self.store.save_stage(review.id, result)
            # -- validation ------------------------------------------------------
            await self.advance(review, "EVIDENCE_VALIDATION")
            findings = []
            for proposal in proposals:
                finding = DocumentFinding(
                    **proposal.model_dump(),
                    id=str(uuid4()),
                    fingerprint="",
                    stage="document_review",
                    provenance=Provenance(
                        agent="document_review",
                        prompt_version=prompt_version(),
                        model=models["document_review"].model,
                        run_id=str(review.id),
                        context_bundle_hash="",
                    ),
                )
                finding.validation = policy.validate(finding, corpus)
                finding.fingerprint = policy.fingerprint(
                    finding.anchor.page_id,
                    finding.category,
                    finding.anchor.quote,
                    finding.claim,
                )
                finding.status = (
                    "validated" if finding.validation.valid else "discarded"
                )
                findings.append(finding)
            findings = policy.deduplicate(findings)
            await record(
                "validation",
                {
                    "proposed": len(proposals),
                    "valid": sum(f.status == "validated" for f in findings),
                },
            )
            # -- verification ---------------------------------------------------
            await self.advance(review, "FINDING_VERIFICATION")
            if llm is not None and models.get("document_verification") is not None:
                await self._verify(
                    review, corpus, findings, config, llm, redactor, models
                )
            else:
                for f in findings:
                    if f.status == "validated" and policy.needs_verification(f):
                        f.status = "downgraded"
            # -- finalization ---------------------------------------------------
            await self.advance(review, "FINALIZATION")
            for f in findings:
                policy.normalize(f)
            await self.store.save_findings(review, findings)
            await self.advance(review, "DECISION")
            decision = self.decide(findings, partial)
            report = await DocumentPublisher(self.docs, self.store).publish(
                review,
                subject,
                findings,
                decision,
                config,
                overrides.report_mode,
                degradations,
                partial,
            )
            snapshot["report"] = report
            snapshot["bundle"]["degradations"] = list(degradations)
            await self.store.save_snapshot(review.id, snapshot)
            final = "PUBLISHED"
            return await self.finish(review, final, decision, partial=partial)
        except StaleReview:
            return await self.finish(
                review,
                "FAILED_CONTEXT",
                decision,
                partial=True,
                error="Page changed during the review",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("document_pipeline_failed", review_id=review_id)
            return await self.finish(
                review,
                "FAILED_INTERNAL",
                decision,
                partial=True,
                error="Internal error",
            )
        finally:
            if llm is not None and hasattr(llm, "close"):
                await llm.close()

    async def _spent(self, review_id):
        from sqlalchemy import func, select

        from reviewer.store.models import LLMCall

        async with self.store.sessions() as session:
            return int(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(LLMCall.tokens_in + LLMCall.tokens_out), 0
                        )
                    ).where(LLMCall.review_id == review_id, LLMCall.stage != "chat")
                )
            )

    async def _review(self, review, corpus, overrides, config, llm, spec):
        units = corpus.units(config.document_review.unit_tokens)
        result = StageResult(stage="document_review", attempts=1)
        proposals = []
        sources = DocumentSources(
            corpus, self.docs, overrides.check_space, corpus.redactor
        )
        semaphore = asyncio.Semaphore(config.document_review.unit_concurrency)
        lock = asyncio.Lock()

        async def one(unit):
            async with semaphore:
                try:
                    envelope, reads = await review_unit(
                        corpus,
                        unit,
                        llm,
                        sources,
                        config,
                        review.id,
                        overrides.instruction,
                        spec,
                    )
                except (BudgetExhausted, StageFailed, TimeoutError) as exc:
                    async with lock:
                        result.skipped.append(unit["id"])
                        result.notes.append(f"{unit['id']}: {type(exc).__name__}")
                    return
                async with lock:
                    if unit["id"] in envelope.coverage.units_examined:
                        result.examined.append(unit["id"])
                    else:
                        result.skipped.append(unit["id"])
                        if envelope.coverage.skip_reason:
                            result.notes.append(
                                f"{unit['id']}: {envelope.coverage.skip_reason}"
                            )
                    proposals.extend(envelope.findings)
                    if envelope.notes_for_summary:
                        result.notes.append(envelope.notes_for_summary[:300])

        async with asyncio.TaskGroup() as group:
            for unit in units:
                group.create_task(one(unit))
        result.partial = bool(result.skipped)
        result.failed = bool(units) and not result.examined
        return proposals, result

    async def _verify(self, review, corpus, findings, config, llm, redactor, models):
        candidates = [
            f
            for f in findings
            if f.status == "validated" and policy.needs_verification(f)
        ]
        semaphore = asyncio.Semaphore(config.verification_concurrency)

        async def one(finding):
            async with semaphore:
                try:
                    await verify(
                        finding,
                        corpus,
                        llm,
                        review.id,
                        redactor,
                        models.get("document_verification"),
                    )
                except Exception:
                    finding.status = "downgraded"

        async with asyncio.TaskGroup() as group:
            for finding in candidates:
                group.create_task(one(finding))

    @staticmethod
    def decide(findings, partial):
        live = [f for f in findings if f.status not in {"discarded", "suppressed"}]
        if partial:
            return "COMMENT_ONLY"
        if any(f.severity_final == Severity.REQUIRED and f.verified for f in live):
            return "REQUEST_CHANGES"
        return "APPROVE" if not live else "COMMENT_ONLY"

    async def advance(self, review, state):
        current = await self.store.get(review.id)
        if current.state in TERMINAL:
            raise StaleReview()
        if ORDER.index(current.state) < ORDER.index(state):
            await self.store.transition(review.id, state)

    async def finish(self, review, state, decision, *, partial=False, error=None):
        logger.info(
            "document_pipeline_finish",
            review_id=review.id,
            state=state,
            decision=decision,
            partial=partial,
        )
        fields = dict(decision=str(decision), partial=partial)
        if error:
            fields["error"] = error
        result = await self.store.transition(review.id, state, **fields)
        # No commit status exists for a page; nothing is left to deliver.
        await self.store.mark_status(review.id)
        return result.state
