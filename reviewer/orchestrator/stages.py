import asyncio
import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel

from reviewer.agents.base import TemplateAgent
from reviewer.findings.models import ProposedFinding
from reviewer.orchestrator.budget import BudgetExhausted
from reviewer.orchestrator.deadlines import cutoff
from reviewer.orchestrator.unit_plan import identity, plan
from reviewer.telemetry.activity import activity, record


class StageResult(BaseModel):
    stage: str
    input_hash: str = ""
    findings: list[ProposedFinding] = []
    examined: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    failed: bool = False
    partial: bool = False
    attempts: int = 0


# Each stage selects its own model by role, so the unit kind is all that is
# fixed in code; the model behind the role is operator configuration.
STAGES = {
    "defect_review": "hunks",
    "purpose": "whole_change",
    "design": "whole_change",
    "correctness": "file_group",
    "complexity": "file_group",
    "tests_": "file_group",
    "line_review": "file",
    "system_context": "whole_change",
}


@activity("agent", lambda name, *args, **kwargs: name)
async def execute(
    name,
    bundle,
    llm,
    config,
    context_provider=None,
    only_paths=None,
    store=None,
    redactor=None,
    previous=None,
):
    kind = STAGES[name]
    agent = TemplateAgent(name, unit_kind=kind)
    spec = getattr(llm, "specs", {}).get(name)
    agent.context_tokens = spec.context_tokens if spec else None
    units, selected = plan(bundle, config, kind, name, only_paths)
    hashes = {u.id: identity(agent, bundle, u, llm, config) for u in units}
    stage_hash = hashlib.sha256("".join(hashes.values()).encode()).hexdigest()
    if previous is not None and previous.input_hash == stage_hash:
        return previous
    cached = await store.unit_results(bundle.review_id, name) if store else {}
    await record(
        "stage_dispatch",
        dict(
            name=name,
            units=len(units),
            selected=len(selected),
            recovered=sum(h in cached for h in hashes.values()),
            concurrency=config.unit_concurrency if kind != "whole_change" else 1,
            triage=False,
        ),
    )
    pending = iter(enumerate(units))
    completed = {}
    exhausted = False
    stage_deadline = cutoff(bundle, config, name)

    async def worker():
        nonlocal exhausted
        for index, unit in pending:
            key = hashes[unit.id]
            if key in cached:
                completed[index] = StageResult.model_validate(cached[key])
                continue
            result = StageResult(stage=name)
            completed[index] = result
            remaining = (stage_deadline - datetime.now(UTC)).total_seconds()
            if exhausted or unit.omitted or unit.id not in selected or remaining <= 0:
                result.partial = True
                result.skipped.append(unit.id)
                reason = (
                    "Source line exceeds chunk allowance"
                    if unit.omitted
                    else "Review token budget exhausted"
                    if exhausted
                    else "Analysis deadline reached"
                )
                result.notes.append(f"{unit.id}: {reason}; coverage is unknown")
                continue
            try:
                # One wall-clock allowance covers context rounds, transport retries,
                # and the single coverage retry. External cancellation still propagates.
                async with asyncio.timeout(min(config.unit_timeout_s, remaining)):
                    for attempt in range(2):
                        result.attempts += 1
                        envelope = await agent.run(
                            bundle,
                            unit,
                            llm,
                            None if name == "line_review" else context_provider,
                            bool(attempt),
                        )
                        result.findings.extend(
                            f
                            for f in envelope.findings
                            if name not in {"correctness", "defect_review"}
                            or f.failure_scenario
                        )
                        if getattr(envelope, "findings_truncated", False):
                            result.partial = True
                            result.notes.append(
                                "Additional findings omitted by the per-unit limit"
                            )
                        if envelope.notes_for_summary:
                            result.notes.append(envelope.notes_for_summary)
                        if (
                            unit.id in envelope.coverage.units_examined
                            and unit.id not in envelope.coverage.units_skipped
                        ):
                            result.examined.append(unit.id)
                            break
            except BudgetExhausted:
                exhausted = True
                result.notes.append(f"{unit.id}: Review token or time budget exhausted")
            except TimeoutError:
                result.notes.append(
                    f"{unit.id}: Unit deadline reached; remaining coverage is unknown"
                )
                await record("unit_deadline", dict(name=name, attempts=result.attempts))
            except Exception:
                result.failed = True
                # Never expose raw upstream errors or reviewed code in diagnostics.
                result.notes.append(
                    f"{unit.id}: Review attempt failed; see audited call outcomes"
                )
            if not result.examined:
                result.skipped.append(unit.id)
                result.partial = True
            if store:
                # Required persistence: errors escape, never masquerade as coverage.
                data = result.model_dump(mode="json")
                if redactor is not None:
                    data = redactor.object(data)
                await store.save_unit(bundle.review_id, name, key, data)

    concurrency = config.unit_concurrency if kind != "whole_change" else 1
    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(units))):
            group.create_task(worker())
    result = StageResult(stage=name, input_hash=stage_hash)
    for index in sorted(completed):
        item = completed[index]
        result.findings.extend(item.findings)
        result.examined.extend(item.examined)
        result.skipped.extend(item.skipped)
        result.notes.extend(item.notes)
        result.attempts += item.attempts
        result.partial |= item.partial
        result.failed |= item.failed
    await record(
        "stage_coverage",
        dict(
            name=name,
            examined=len(result.examined),
            skipped=len(result.skipped),
            attempts=result.attempts,
            failed=result.failed,
            partial=result.partial,
        ),
    )
    return result


async def fan_out(bundle, llm, config, context_provider=None, only_paths=None):
    names = ["correctness", "complexity", "tests_", "line_review"]
    if config.analysis_mode == "standard":
        names = ["defect_review"]
    results = await asyncio.gather(
        *(execute(n, bundle, llm, config, context_provider, only_paths) for n in names),
        return_exceptions=True,
    )
    return [
        r
        if isinstance(r, StageResult)
        else StageResult(stage=n, failed=True, partial=True)
        for n, r in zip(names, results)
    ]
