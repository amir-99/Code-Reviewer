import json
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field, create_model

from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import Coverage
from reviewer.services.llm import tokens
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


STAGE = "defect_review"
CONTRACT = "_compact"


def unit_response_model(unit_id, max_findings=3):
    from reviewer.agents.defects import DefectClaim, DefectEnvelope

    # Constrain IDs in the gateway schema as well as checking coverage in code.
    # Empty coverage stays representable and triggers the one coverage retry.
    coverage = create_model(
        "UnitCoverage",
        __base__=Coverage,
        units_examined=(list[Literal[unit_id]], ...),
        units_skipped=(list[Literal[unit_id]], ...),
    )
    # The per-unit cap grows with the unit, so packing more code into one call
    # does not crowd out findings a smaller unit would have had room for.
    return create_model(
        "UnitStageEnvelope",
        __base__=DefectEnvelope,
        coverage=(coverage, ...),
        findings=(list[DefectClaim], Field(max_length=max_findings)),
    )


class DefectReviewAgent:
    """The combined Defect Review over one unit, with one context round."""

    name = STAGE
    # The model-selection role this agent's calls resolve through.
    tier = STAGE
    context_tokens: int | None = None
    rounds = 2

    def __init__(self, document_tokens=6000):
        self.document_tokens = document_tokens
        self.prompts = {}
        self.condensed = {}

    @property
    def prompt_version(self):
        # The shared contract is versioned independently, so record both.
        return f"{PROMPTS[STAGE][0]}+shared.{PROMPTS[CONTRACT][0]}"

    def requirements(self, bundle):
        """Condensed once per review: every unit shares the same requirements."""
        from reviewer.context.digest import condense

        key = id(bundle)
        if key not in self.condensed:
            self.condensed[key], omitted = condense(bundle, self.document_tokens)
            if omitted and "requirements_condensed" not in bundle.degradations:
                bundle.degradations.append("requirements_condensed")
        return self.condensed[key]

    def build_prompt(self, bundle, unit):
        # Unit identity and the call itself both need the prompt; build it once.
        key = (
            id(bundle),
            unit.id,
            unit.content,
            json.dumps(unit.planned, sort_keys=True),
        )
        if key not in self.prompts:
            self.prompts[key] = self.compose(bundle, unit)
        return self.prompts[key]

    def compose(self, bundle, unit):
        version, template, digest = PROMPTS[STAGE]
        requirements = self.requirements(bundle)
        system = template + "\n" + PROMPTS[CONTRACT][1] + "\n" + INJECTION_RULE
        static = [s.model_dump() for s in bundle.static]
        meta = {"unit": unit.model_dump(exclude={"omitted", "content", "planned"})}

        def render(requirements):
            # What every unit of the review shares comes first, so a gateway's
            # prefix cache can reuse it; the unit's own code comes last, as
            # numbered lines rather than a JSON string of escaped newlines.
            return "\n".join(
                [
                    frame(
                        json.dumps(
                            {"requirements": requirements, "static": static},
                            ensure_ascii=False,
                        ),
                        "review-context",
                    ),
                    frame(json.dumps(meta, ensure_ascii=False), "review-unit"),
                    *(
                        [
                            frame(
                                json.dumps(unit.planned, ensure_ascii=False),
                                "planned-context",
                            )
                        ]
                        if unit.planned
                        else []
                    ),
                    frame(unit.content, "review-diff"),
                ]
            )

        # Use the receiving model's window instead of a universal 12 KB cutoff.
        # Leave a quarter of that window for output; this sizes input only and
        # does not impose a generation cap. Fakes without model metadata retain
        # the historical conservative requirements allocation.
        encoded = json.dumps(requirements, ensure_ascii=False)
        user = render(requirements)
        input_limit = self.context_tokens * 3 // 4 if self.context_tokens else None

        def fits(user, size):
            if input_limit is None:
                return size <= 12000
            return tokens.messages(system, user) <= input_limit

        raw = encoded.encode()
        if not fits(user, len(raw)):
            low, high = 0, len(raw)
            while low < high:
                middle = (low + high + 1) // 2
                candidate = render(
                    {"truncated_requirements": raw[:middle].decode(errors="ignore")}
                )
                if fits(candidate, middle):
                    low = middle
                else:
                    high = middle - 1
            user = render({"truncated_requirements": raw[:low].decode(errors="ignore")})
            if "prompt_requirements_truncated" not in bundle.degradations:
                bundle.degradations.append("prompt_requirements_truncated")
        return system, user

    @activity("unit", lambda self, *args, **kwargs: self.name)
    async def run(
        self,
        bundle,
        unit,
        llm,
        context_provider=None,
        coverage_retry=False,
        observed=None,
    ):
        """Review one unit. `observed`, when given, receives each round's
        envelope as it arrives, so a caller whose deadline cancels a later
        context round still holds the earlier, complete answer."""
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
        response_model = unit_response_model(unit.id, getattr(unit, "max_findings", 3))
        rounds = self.rounds
        for round_no in range(rounds):
            result = await llm.complete(
                stage=self.name,
                tier=self.tier,
                system=system,
                user=user,
                response_model=response_model,
                review_id=bundle.review_id,
                timeout_s=180,
                prompt_version=self.prompt_version,
            )
            if hasattr(result, "expand"):
                result = result.expand()
            if observed is not None:
                observed.append(result)
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
            # Additional rounds share the receiving model's input allowance.
            # Trim intact numbered excerpts, never silently slice source text.
            from reviewer.context.excerpts import shorten

            limit = self.context_tokens * 3 // 4 if self.context_tokens else 21000
            while tokens.messages(system, user) > limit:
                candidates = [
                    path
                    for path, value in context.items()
                    if isinstance(value, dict) and value.get("code")
                ]
                if not candidates:
                    break
                path = max(candidates, key=lambda p: len(context[p]["code"]))
                context[path] = shorten(context[path])
                user = (
                    initial_user
                    + "\n"
                    + frame(json.dumps(context), "requested-code-context")
                )
        return result
