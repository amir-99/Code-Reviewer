"""Let a cheap model lower a finding's code-derived severity, never raise it.

Category alone makes every correctness claim REQUIRED, so a cosmetic slip and a
broken build read alike. One batched call judges each published candidate's
failure; code applies a judgement only when it is lower. Gating stays
deterministic: in gating mode a verified finding that would fail the check is
never lowered, because a model that can be argued down by the merge request's
own text must not be able to pass it.
"""

import json
from typing import Literal

from pydantic import Field

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import ImpactLevel, Schema, Severity, SeverityTriage
from reviewer.telemetry.activity import activity, record

ROLE = "severity_triage"
ORDER = list(Severity)
BATCH = 20
# Stages whose severities triage may lower; secrets stay deterministic.
STAGES = {"defect_review", "build_agent"}


class Judgement(Schema):
    finding: str
    severity: Literal["keep", "SUGGESTION", "NIT", "FYI"]
    impact_level: ImpactLevel | None
    reason: str = Field(max_length=200)


class TriageResult(Schema):
    judgements: list[Judgement]


def candidates(findings):
    return [
        f
        for f in findings
        if f.stage in STAGES
        and f.status not in {"discarded", "suppressed", "resolved"}
        and f.severity_final in {Severity.REQUIRED, Severity.SUGGESTION}
    ]


def gates(f, config):
    """Whether this finding can fail a gating check (decision/engine.py)."""
    return (
        config.enforcement == "gating"
        and f.verified
        and (
            f.severity_final == Severity.BLOCKER
            or (
                f.severity_final == Severity.REQUIRED
                and f.anchor.introduced_by_this_change
            )
        )
    )


def apply(f, judgement, config):
    """Record the judgement; lower the severity only when that is allowed."""
    current = f.severity_final
    proposed = current if judgement.severity == "keep" else Severity(judgement.severity)
    lower = ORDER.index(proposed) > ORDER.index(current)
    applied = lower and not gates(f, config)
    f.triage = SeverityTriage(
        from_severity=current,
        to_severity=proposed if applied else current,
        proposed=proposed,
        reason=judgement.reason,
        applied=applied,
    )
    if applied:
        f.severity_final = proposed
    if f.impact_level is None and judgement.impact_level is not None:
        f.impact_level = judgement.impact_level
    return applied


class SeverityTriageAgent:
    name = ROLE
    tier = ROLE

    @property
    def prompt_version(self):
        return PROMPTS[ROLE][0]

    async def evidence(self, f, lookup):
        if lookup is None:
            return None
        try:
            found = await lookup.file(
                f.anchor.file, max(1, f.anchor.line_start - 3), f.anchor.line_end + 3
            )
        except Exception:
            return None
        value = found.get(f.anchor.file) or {}
        return (value.get("code") or "")[:1500] or None

    @activity("phase", "Triage severity")
    async def triage(self, findings, bundle, llm, config, context_provider=None):
        """Lower severities in place; failures leave code's severity standing."""
        spec = getattr(llm, "specs", {}).get(ROLE)
        if not config.review.severity_triage or llm is None or spec is None:
            return
        pending = candidates(findings)
        if not pending:
            return
        lookup = getattr(context_provider, "lookup", None)
        system = PROMPTS[ROLE][1] + "\n" + INJECTION_RULE
        lowered = 0
        try:
            for start in range(0, len(pending), BATCH):
                batch = {
                    f"f{start + i + 1}": f
                    for i, f in enumerate(pending[start : start + BATCH])
                }
                items = [
                    {
                        "id": key,
                        "category": f.category,
                        "severity": str(f.severity_final),
                        "claim": f.claim,
                        "failure_scenario": f.failure_scenario or f.reason,
                        "impact": f.impact,
                        "file": f.anchor.file,
                        "lines": [f.anchor.line_start, f.anchor.line_end],
                        "code": await self.evidence(f, lookup),
                    }
                    for key, f in batch.items()
                ]
                result = await llm.complete(
                    stage=ROLE,
                    tier=ROLE,
                    system=system,
                    user=frame(json.dumps(items, ensure_ascii=False), "findings"),
                    response_model=TriageResult,
                    review_id=bundle.review_id,
                    timeout_s=120,
                    prompt_version=self.prompt_version,
                )
                seen = set()
                for judgement in result.judgements:
                    f = batch.get(judgement.finding)
                    if f is None or judgement.finding in seen:
                        continue
                    seen.add(judgement.finding)
                    lowered += apply(f, judgement, config)
        except Exception:
            # Judgements already applied stand; the rest keep code's severity.
            bundle.degradations.append("severity_triage_unavailable")
        await record(
            "severity_triage", dict(name=ROLE, judged=len(pending), lowered=lowered)
        )
