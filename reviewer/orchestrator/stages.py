import asyncio
import hashlib
from datetime import UTC, datetime

from pydantic import BaseModel

from reviewer.agents.base import DefectReviewAgent
from reviewer.findings.models import ProposedFinding
from reviewer.orchestrator.budget import BudgetExhausted
from reviewer.orchestrator.deadlines import cutoff
from reviewer.orchestrator.unit_plan import identity, plan
from reviewer.telemetry.activity import activity, parent, record


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
    agent = DefectReviewAgent(config.documents.prompt_tokens)
    spec = getattr(llm, "specs", {}).get(name)
    agent.context_tokens = spec.context_tokens if spec else None
    units, selected = plan(bundle, config, only_paths)
    hashes = {u.id: identity(agent, bundle, u, llm, config) for u in units}
    stage_hash = hashlib.sha256("".join(hashes.values()).encode()).hexdigest()
    progress = dict(
        total=len(units), completed=0, running=0, idle=len(units), stopped=0
    )

    revision = 0

    async def report_progress():
        nonlocal revision
        revision += 1
        await record(
            "unit_progress",
            dict(name=name, execution_id=parent.get(), revision=revision, **progress),
        )

    if previous is not None and previous.input_hash == stage_hash:
        progress["completed"] = min(len(units), len(set(previous.examined)))
        progress["stopped"] = len(units) - progress["completed"]
        progress["idle"] = 0
        await report_progress()
        return previous
    cached = await store.unit_results(bundle.review_id, name) if store else {}
    await record(
        "stage_dispatch",
        dict(
            name=name,
            units=len(units),
            selected=len(selected),
            recovered=sum(h in cached for h in hashes.values()),
            concurrency=config.unit_concurrency,
            triage=False,
        ),
    )
    await report_progress()
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
                progress["idle"] -= 1
                progress["completed" if completed[index].examined else "stopped"] += 1
                await report_progress()
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
                progress["idle"] -= 1
                progress["stopped"] += 1
                await report_progress()
                continue
            progress["idle"] -= 1
            progress["running"] += 1
            await report_progress()
            try:
                # One wall-clock allowance covers context rounds, transport retries,
                # and the single coverage retry. External cancellation still propagates.
                async with asyncio.timeout(min(config.unit_timeout_s, remaining)):
                    for attempt in range(2):
                        result.attempts += 1
                        envelope = await agent.run(
                            bundle, unit, llm, context_provider, bool(attempt)
                        )
                        result.findings.extend(
                            f for f in envelope.findings if f.failure_scenario
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
                        if unit.id in envelope.coverage.units_skipped:
                            # An explicit skip answers the coverage question;
                            # only missing coverage earns the one retry.
                            break
            except asyncio.CancelledError:
                progress["running"] -= 1
                progress["stopped"] += 1
                await report_progress()
                raise
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
            progress["running"] -= 1
            progress["completed" if result.examined else "stopped"] += 1
            await report_progress()
            if store:
                # Required persistence: errors escape, never masquerade as coverage.
                data = result.model_dump(mode="json")
                if redactor is not None:
                    data = redactor.object(data)
                await store.save_unit(bundle.review_id, name, key, data)

    async with asyncio.TaskGroup() as group:
        for _ in range(min(config.unit_concurrency, len(units))):
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
