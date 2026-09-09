import asyncio

from pydantic import BaseModel

from reviewer.agents.base import TemplateAgent
from reviewer.context.partition import partition
from reviewer.findings.models import ProposedFinding
from reviewer.orchestrator.budget import BudgetExhausted
from reviewer.telemetry.activity import activity


class StageResult(BaseModel):
    stage: str
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
    "purpose": "whole_change",
    "design": "whole_change",
    "correctness": "file_group",
    "complexity": "file_group",
    "tests_": "file_group",
    "line_review": "file",
    "system_context": "whole_change",
}


@activity("agent", lambda name, *args, **kwargs: name)
async def execute(name, bundle, llm, config, context_provider=None, only_paths=None):
    kind = STAGES[name]
    agent = TemplateAgent(name, unit_kind=kind)
    units = partition(bundle, kind, config.review.unit_tokens)
    if only_paths is not None:
        units = [u for u in units if set(u.paths) & set(only_paths)]
    # Workers consume the iterator without awaiting between reads. Keep results
    # in dispatch order, regardless of model completion order.
    pending = iter(enumerate(units))
    completed = {}
    exhausted = False

    async def worker():
        nonlocal exhausted
        for index, unit in pending:
            result = StageResult(stage=name)
            completed[index] = result
            if exhausted:
                result.partial = True
                result.skipped.append(unit.id)
                continue
            for attempt in range(2):
                result.attempts += 1
                try:
                    envelope = await agent.run(
                        bundle,
                        unit,
                        llm,
                        None if name == "line_review" else context_provider,
                    )
                    result.findings.extend(
                        f
                        for f in envelope.findings
                        if name != "correctness" or f.failure_scenario
                    )
                    if envelope.notes_for_summary:
                        result.notes.append(envelope.notes_for_summary)
                    if unit.id in envelope.coverage.units_examined:
                        result.examined.append(unit.id)
                        break
                except BudgetExhausted:
                    exhausted = True
                    break
                except Exception:
                    result.failed = True
                    break
            if not result.examined:
                result.skipped.append(unit.id)
                result.partial = True

    # TaskGroup drains cancelled children before the pipeline cleans its worktree.
    concurrency = config.unit_concurrency if kind != "whole_change" else 1
    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(units))):
            group.create_task(worker())
    result = StageResult(stage=name)
    for index in sorted(completed):
        item = completed[index]
        result.findings.extend(item.findings)
        result.examined.extend(item.examined)
        result.skipped.extend(item.skipped)
        result.notes.extend(item.notes)
        result.attempts += item.attempts
        result.partial |= item.partial
        result.failed |= item.failed
    return result


async def fan_out(bundle, llm, config, context_provider=None, only_paths=None):
    names = ["correctness", "complexity", "tests_", "line_review"]
    if "triage_mode" in bundle.degradations:
        names = ["tests_"]
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
