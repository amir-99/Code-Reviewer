import json
from abc import ABC, abstractmethod
from hashlib import sha256
from pathlib import Path

from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import StageEnvelope
from reviewer.telemetry.activity import activity

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


class StageAgent(ABC):
    name: str
    tier: str = "strong"
    unit_kind: str = "file_group"

    @property
    def prompt_version(self):
        # The shared contract is versioned independently, so record both.
        return f"{PROMPTS[self.name][0]}+shared.{PROMPTS[SHARED][0]}"

    @abstractmethod
    def build_prompt(self, bundle, unit): ...
    @activity("unit", "Review work unit")
    async def run(self, bundle, unit, llm, context_provider=None):
        system, user = self.build_prompt(bundle, unit)
        for round_no in range(3):
            result = await llm.complete(
                stage=self.name,
                tier=self.tier,
                system=system,
                user=user,
                response_model=StageEnvelope,
                review_id=bundle.review_id,
                max_tokens=16000,
                timeout_s=90,
                prompt_version=self.prompt_version,
            )
            if not result.context_requests or not context_provider or round_no == 2:
                return result
            extra = await context_provider(result.context_requests)
            user += "\n" + frame(json.dumps(extra), "requested-code-context")
        return result


class TemplateAgent(StageAgent):
    def __init__(self, name, tier="strong", unit_kind="file_group"):
        self.name, self.tier, self.unit_kind = name, tier, unit_kind

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
                    "unit": unit.model_dump(),
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
            template + "\n" + PROMPTS[SHARED][1] + "\n" + INJECTION_RULE,
            user,
        )
