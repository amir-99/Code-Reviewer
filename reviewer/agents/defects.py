"""Small wire contract; deterministic metadata stays in the internal finding."""

from typing import Literal

from pydantic import Field

from reviewer.findings.models import (
    Anchor,
    Confidence,
    ContextRequest,
    Coverage,
    Evidence,
    ImpactLevel,
    ProposedFinding,
    Schema,
    StageEnvelope,
)


class DefectAnchor(Schema):
    file: str
    line_start: int
    line_end: int
    symbol: str | None = None


class DefectClaim(Schema):
    anchor: DefectAnchor
    category: Literal[
        "security",
        "data_integrity",
        "prompt_injection",
        "correctness",
        "concurrency",
        "error_handling",
        "test_gap",
    ]
    claim: str = Field(max_length=200)
    failure_scenario: str = Field(min_length=1, max_length=300)
    impact: str = Field(max_length=300)
    impact_level: ImpactLevel | None
    evidence: list[DefectAnchor] = Field(min_length=1, max_length=2)
    suggested_direction: str = Field(max_length=200)
    confidence: Confidence
    requirement_ref: str | None = None

    def expand(self):
        data = self.model_dump(exclude={"evidence"})
        data["anchor"] = Anchor(**data["anchor"])
        return ProposedFinding(
            **data,
            reason=self.failure_scenario,
            severity_proposed="REQUIRED",
            evidence=[
                Evidence(**e.model_dump(exclude={"symbol"}), note="")
                for e in self.evidence
            ],
        )


class ExpandedDefectEnvelope(StageEnvelope):
    findings_truncated: bool


class DefectEnvelope(Schema):
    findings: list[DefectClaim] = Field(max_length=3)
    findings_truncated: bool
    context_requests: list[ContextRequest] = Field(default=[], max_length=3)
    coverage: Coverage
    notes_for_summary: str = Field(default="", max_length=300)

    def expand(self):
        return ExpandedDefectEnvelope(
            findings=[f.expand() for f in self.findings],
            findings_truncated=self.findings_truncated,
            context_requests=self.context_requests,
            coverage=self.coverage,
            notes_for_summary=self.notes_for_summary,
        )
