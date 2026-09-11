import json
from abc import ABC, abstractmethod
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import create_model

from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import Coverage, StageEnvelope
from reviewer.telemetry.activity import activity, record

PROMPTS = {}
for path in sorted(
    (Path(__file__).parent / "prompts").glob("*/v*.md"),
    key=lambda p: tuple(int(x) for x in p.stem[1:].split(".")),
):
    stage = path.parent.name
    PROMPTS[stage] = (
        path.stem[1:],
        path.read_text(),
        sha256(path.read_bytes()).hexdigest(),
    )


SHARED = "_shared"


def unit_response_model(unit_id, compact=False):
    from reviewer.agents.defects import DefectEnvelope

    # Constrain IDs in the gateway schema as well as checking coverage in code.
    # Empty coverage stays representable and triggers the one coverage retry.
    coverage = create_model(
        "UnitCoverage",
        __base__=Coverage,
        units_examined=(list[Literal[unit_id]], ...),
        units_skipped=(list[Literal[unit_id]], ...),
    )
    return create_model(
        "UnitStageEnvelope",
        __base__=DefectEnvelope if compact else StageEnvelope,
        coverage=(coverage, ...),
    )


class StageAgent(ABC):
    name: str
    # The model-selection role this agent's calls resolve through. A stage runs
    # on its own role by default, so operators can price each stage separately.
    tier: str = ""
    unit_kind: str = "file_group"
    max_output_tokens: int = 4096

    @property
    def prompt_version(self):
        # The shared contract is versioned independently, so record both.
        shared = "_compact" if self.name == "defect_review" else SHARED
        return f"{PROMPTS[self.name][0]}+shared.{PROMPTS[shared][0]}"

    @abstractmethod
    def build_prompt(self, bundle, unit): ...
    @activity("unit", lambda self, *args, **kwargs: self.name)
    async def run(self, bundle, unit, llm, context_provider=None, coverage_retry=False):
        system, user = self.build_prompt(bundle, unit)
        if coverage_retry:
            user += "\n" + frame(
                json.dumps(
                    {
                        "coverage_retry": True,
                        "expected_unit_id": unit.id,
                        "instruction": "Return coverage for this exact unit ID; mark skipped if not examined.",
                    }
                ),
                "coverage-retry",
            )
        initial_user = user
        context = {}
        compact = self.name == "defect_review"
        response_model = unit_response_model(unit.id, compact)
        rounds = 2 if compact else 3
        for round_no in range(rounds):
            result = await llm.complete(
                stage=self.name,
                tier=self.tier or self.name,
                system=system,
                user=user,
                response_model=response_model,
                review_id=bundle.review_id,
                max_tokens=self.max_output_tokens,
                timeout_s=90,
                prompt_version=self.prompt_version,
            )
            if compact and hasattr(result, "expand"):
                result = result.expand()
            if unit.id not in result.coverage.units_examined:
                await record(
                    "coverage_mismatch",
                    dict(
                        name=self.name,
                        context_round=round_no + 1,
                        coverage_retry=coverage_retry,
                        reported_examined=len(result.coverage.units_examined),
                        explicitly_skipped=unit.id in result.coverage.units_skipped,
                    ),
                )
            if (
                not result.context_requests
                or not context_provider
                or round_no == rounds - 1
            ):
                return result
            extra = await context_provider(result.context_requests)
            if extra and all(
                context.get(path) == value for path, value in extra.items()
            ):
                return result
            context.update(extra)
            user = (
                initial_user
                + "\n"
                + frame(json.dumps(context), "requested-code-context")
            )
        return result


class TemplateAgent(StageAgent):
    def __init__(self, name, tier=None, unit_kind="file_group"):
        self.name, self.tier, self.unit_kind = name, tier or name, unit_kind

    def build_prompt(self, bundle, unit):
        version, template, digest = PROMPTS[self.name]
        requirements = {
            "issue": bundle.issue.model_dump() if bundle.issue else None,
            "epic": bundle.epic.model_dump() if bundle.epic else None,
            "documents": [d.model_dump() for d in bundle.documents],
            "mr_title": bundle.mr.title,
            "mr_description": bundle.mr.description,
        }
        # Keep requirement context within a conservative shared prompt allocation.
        encoded = json.dumps(requirements)
        if len(encoded.encode()) > 12000:
            requirements = {
                "truncated_requirements": encoded.encode()[:12000].decode(
                    errors="replace"
                )
            }
            if "prompt_requirements_truncated" not in bundle.degradations:
                bundle.degradations.append("prompt_requirements_truncated")
        user = frame(
            json.dumps(
                {
                    "unit": unit.model_dump(exclude={"omitted"}),
                    "requirements": requirements,
                    "aggregated_findings": bundle.aggregated_findings
                    if self.name == "system_context"
                    else [],
                    "static": [s.model_dump() for s in bundle.static],
                }
            ),
            "review-context",
        )
        return (
            template
            + "\n"
            + PROMPTS["_compact" if self.name == "defect_review" else SHARED][1]
            + "\n"
            + INJECTION_RULE,
            user,
        )
