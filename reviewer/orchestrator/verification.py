"""Bounded independent verification after validation and deduplication."""

import asyncio

from reviewer.agents.verification import verify
from reviewer.findings.models import ContextRequest, VerificationResult
from reviewer.findings.policy import needs_verification
from reviewer.orchestrator.budget import BudgetExhausted


def unavailable(finding, bundle, error):
    if isinstance(error, BudgetExhausted):
        if "budget_exhausted" not in bundle.degradations:
            bundle.degradations.append("budget_exhausted")
    finding.verification = VerificationResult(
        verdict="uncertain",
        counterargument="Verification unavailable",
        reasoning="Cannot confirm within budget",
    )


async def verify_findings(
    findings, bundle, llm, redactor, context_provider, concurrency
):
    ready = []
    # Scan every candidate's cited context before any verifier prompt is built.
    # This also ensures secrets discovered for later candidates redact earlier ones.
    for finding in findings:
        if not needs_verification(finding) or (
            finding.validation and not finding.validation.evidence_valid
        ):
            continue
        try:
            requests = [
                ContextRequest(kind="file", target=path, reason="verification evidence")
                for path in dict.fromkeys(
                    [finding.anchor.file] + [e.file for e in finding.evidence]
                )
            ]
            # The context provider accepts at most five requests per invocation.
            for offset in range(0, len(requests), 5):
                await context_provider(requests[offset : offset + 5])
        except Exception as error:
            unavailable(finding, bundle, error)
        else:
            ready.append(finding)

    pending = iter(ready)

    async def worker():
        for finding in pending:
            try:
                await verify(finding, bundle, llm, redactor)
            except Exception as error:
                unavailable(finding, bundle, error)

    # Mutate the original findings in place: completion order never reorders them.
    # TaskGroup drains cancellation before the caller cleans up the worktree.
    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(ready))):
            group.create_task(worker())
