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


STAGES = {
    "purpose": ("strong", "whole_change"),
    "design": ("strong", "whole_change"),
    "correctness": ("strong", "file_group"),
    "complexity": ("fast", "file_group"),
    "tests_": ("strong", "file_group"),
    "line_review": ("fast", "file"),
    "system_context": ("strong", "whole_change"),
}


@activity("agent", lambda name, *args, **kwargs: name)
async def execute(name, bundle, llm, config, context_provider=None, only_paths=None):
    tier, kind = STAGES[name]
    agent = TemplateAgent(name, tier, kind)
    units = partition(bundle, kind, config.review.unit_tokens)
    if only_paths is not None:
        units = [u for u in units if set(u.paths) & set(only_paths)]
    result = StageResult(stage=name)
    for unit in units:
        examined = False
        for attempt in range(2):
            result.attempts += 1
            try:
                envelope = await agent.run(
                    bundle,
                    unit,
                    llm,
                    None if name == "line_review" else context_provider,
                )
                if unit.id in envelope.coverage.units_examined:
                    result.examined.append(unit.id)
                    examined = True
                result.findings.extend(
                    f
                    for f in envelope.findings
                    if name != "correctness" or f.failure_scenario
                )
                if envelope.notes_for_summary:
                    result.notes.append(envelope.notes_for_summary)
                if examined:
                    break
            except BudgetExhausted:
                result.partial = True
                result.skipped.extend(
                    u.id for u in units if u.id not in result.examined
                )
                return result
            except Exception:
                result.failed = True
                result.partial = True
                break
        if not examined:
            result.skipped.append(unit.id)
            result.partial = True
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
