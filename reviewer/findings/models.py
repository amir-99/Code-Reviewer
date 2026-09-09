from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Severity(StrEnum):
    BLOCKER = "BLOCKER"
    REQUIRED = "REQUIRED"
    SUGGESTION = "SUGGESTION"
    NIT = "NIT"
    QUESTION = "QUESTION"
    FYI = "FYI"
    PRAISE = "PRAISE"


class ImpactLevel(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Anchor(Schema):
    file: str
    line_start: int
    line_end: int
    symbol: str | None = None
    commit_sha: str = ""
    context_hash: str = ""
    in_diff: bool = False
    introduced_by_this_change: bool = False


class Evidence(Schema):
    file: str
    line_start: int
    line_end: int
    note: str


class ProposedFinding(Schema):
    anchor: Anchor
    category: str
    severity_proposed: Severity
    claim: str = Field(max_length=200)
    reason: str = Field(max_length=600)
    impact: str = Field(max_length=600)
    # Advisory only; null supports historical records and non-defect findings.
    impact_level: ImpactLevel | None = None
    failure_scenario: str | None = None
    evidence: list[Evidence] = Field(min_length=1)
    suggested_direction: str = Field(max_length=600)
    requirement_ref: str | None = None
    confidence: Confidence


class Provenance(Schema):
    agent: str
    prompt_version: str
    model: str
    run_id: str
    context_bundle_hash: str


class ValidationResult(Schema):
    valid: bool
    reasons: list[str] = []
    evidence_valid: bool = True


class VerificationResult(Schema):
    # Ordered so the model argues before it rules: strict JSON schema emits
    # properties in declaration order.
    counterargument: str
    reasoning: str
    verdict: Literal["confirmed", "rejected", "uncertain"]


class RecheckResult(Schema):
    """One previously published finding, judged against a newer head.

    Ordered so the model describes the change before ruling on it: strict JSON
    emits properties in declaration order.
    """

    change_summary: str = Field(default="", max_length=400)
    reasoning: str = Field(default="", max_length=600)
    verdict: Literal[
        "fixed", "partially_fixed", "not_fixed", "obsolete", "unverifiable"
    ]
    evidence: list[Evidence] = []


# Verdicts that answer the comment for good; anything else leaves the thread
# open for a person to settle.
RECHECK_RESOLVING = {"fixed", "obsolete"}


class Finding(ProposedFinding):
    id: str
    fingerprint: str
    stage: str
    provenance: Provenance
    severity_final: Severity | None = None
    validation: ValidationResult | None = None
    verification: VerificationResult | None = None
    status: Literal[
        "proposed",
        "discarded",
        "downgraded",
        "verified",
        "suppressed",
        "published",
        "resolved",
    ] = "proposed"
    resolution: Literal["actioned", "dismissed", "ignored"] | None = None

    @property
    def verified(self):
        return bool(self.verification and self.verification.verdict == "confirmed")


class Coverage(Schema):
    units_examined: list[str]
    units_skipped: list[str]
    skip_reason: str | None


class ContextRequest(Schema):
    kind: Literal["symbol", "file", "callers", "tests_for"]
    target: str
    reason: str


class StageEnvelope(Schema):
    findings: list[ProposedFinding]
    context_requests: list[ContextRequest] = Field(max_length=5, default=[])
    coverage: Coverage
    notes_for_summary: str = Field(default="", max_length=800)
