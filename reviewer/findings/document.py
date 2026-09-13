"""Findings about a page: anchored to a quoted passage, not a file and line.

Code owns the anchor exactly as it does for code: a quote that does not occur
in the section the model named is not a finding, and the fingerprint is
computed here, never taken from the model.
"""

import hashlib
import re
from typing import Literal

from pydantic import Field

from reviewer.findings.dedup import ORDER, normalize_claim
from reviewer.findings.models import (
    Confidence,
    Coverage,
    ImpactLevel,
    Provenance,
    Schema,
    Severity,
    ValidationResult,
    VerificationResult,
)

QUOTE_MIN, QUOTE_MAX = 8, 600

# Document categories and the severity each starts from. Blockers are advisory
# under the current policy, as they are for code.
BASE = {
    "accuracy": Severity.REQUIRED,
    "consistency": Severity.REQUIRED,
    "security": Severity.REQUIRED,
    "prompt_injection": Severity.BLOCKER,
    "completeness": Severity.SUGGESTION,
    "outdated": Severity.SUGGESTION,
    "clarity": Severity.SUGGESTION,
    "structure": Severity.SUGGESTION,
    "terminology": Severity.NIT,
    "style": Severity.NIT,
}
CATEGORIES = tuple(BASE)


class DocumentAnchor(Schema):
    page_id: str = Field(max_length=32)
    # "Overview > Goals"; "(intro)" for text before the first heading.
    heading_path: str = Field(max_length=500)
    quote: str = Field(min_length=QUOTE_MIN, max_length=QUOTE_MAX)


class RelatedPassage(DocumentAnchor):
    """A passage on the same or another page the claim compares against."""

    note: str = Field(default="", max_length=300)


class ProposedDocumentFinding(Schema):
    anchor: DocumentAnchor
    category: str = Field(max_length=40)
    severity_proposed: Severity
    claim: str = Field(max_length=200)
    reason: str = Field(max_length=600)
    impact: str = Field(max_length=600)
    impact_level: ImpactLevel | None = None
    suggested_direction: str = Field(max_length=600)
    confidence: Confidence
    related: list[RelatedPassage] = Field(default_factory=list, max_length=5)


class DocumentFinding(ProposedDocumentFinding):
    id: str
    fingerprint: str
    stage: str
    provenance: Provenance
    severity_final: Severity | None = None
    validation: ValidationResult | None = None
    verification: VerificationResult | None = None
    status: Literal[
        "proposed", "validated", "verified", "downgraded", "discarded", "suppressed"
    ] = "proposed"
    resolution: Literal["actioned", "dismissed", "ignored"] | None = None

    @property
    def verified(self):
        return bool(self.verification and self.verification.verdict == "confirmed")


class DocumentRequest(Schema):
    kind: Literal["search", "section", "page", "space_search", "comments"]
    target: str = Field(default="", max_length=300)
    query: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=300)


class DocumentEnvelope(Schema):
    findings: list[ProposedDocumentFinding] = Field(default_factory=list, max_length=12)
    context_requests: list[DocumentRequest] = Field(default_factory=list, max_length=5)
    coverage: Coverage
    notes_for_summary: str = Field(default="", max_length=800)


def squash(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def fingerprint(page_id, category, quote, claim):
    return hashlib.sha256(
        f"{page_id}|{category}|{normalize_claim(quote)}|{normalize_claim(claim)}".encode()
    ).hexdigest()[:32]


def locate(corpus, passage):
    """The section of the page that contains the quote, or None.

    The named heading is tried first; a quote found elsewhere on the page is
    accepted and the heading corrected, because a model that misreads a heading
    has still pointed at real text.
    """
    sections = corpus.sections_for(passage.page_id)
    if not sections:
        return None
    needle = squash(passage.quote)
    named = [
        s for s in sections if squash(s.heading_path) == squash(passage.heading_path)
    ]
    for section in named + [s for s in sections if s not in named]:
        if needle in squash(section.text):
            return section
    return None


def validate(finding, corpus):
    """Anchor the proposal to real text and set its validation result."""
    reasons = []
    if finding.category not in BASE:
        finding.category = "clarity"
        reasons.append("unknown category normalised")
    section = locate(corpus, finding.anchor)
    if section is None:
        return ValidationResult(
            valid=False, reasons=["quote not found on the page"], evidence_valid=False
        )
    if section.page_id != corpus.subject_page_id:
        return ValidationResult(
            valid=False,
            reasons=["anchor is not on the reviewed page"],
            evidence_valid=False,
        )
    finding.anchor.heading_path = section.heading_path
    kept = []
    for passage in finding.related:
        found = locate(corpus, passage)
        if found is None:
            reasons.append("related passage not found")
            continue
        passage.heading_path = found.heading_path
        kept.append(passage)
    finding.related = kept
    if finding.category == "consistency" and not kept:
        reasons.append("consistency claim without a located counterpart")
        return ValidationResult(valid=False, reasons=reasons, evidence_valid=False)
    return ValidationResult(valid=True, reasons=reasons, evidence_valid=True)


def needs_verification(finding):
    return BASE.get(finding.category, Severity.SUGGESTION) in {
        Severity.BLOCKER,
        Severity.REQUIRED,
    } or finding.severity_proposed in {Severity.BLOCKER, Severity.REQUIRED}


def normalize(finding):
    """Final severity from category, verification and confidence."""
    if finding.status in {"discarded", "suppressed"}:
        return finding
    severity = BASE.get(finding.category, Severity.SUGGESTION)
    if (
        (needs_verification(finding) and not finding.verified)
        or finding.confidence == Confidence.LOW
        or (finding.validation and not finding.validation.evidence_valid)
    ) and severity in {Severity.BLOCKER, Severity.REQUIRED}:
        severity = Severity.SUGGESTION
    if severity == Severity.BLOCKER:
        severity = Severity.SUGGESTION
    finding.severity_final = severity
    return finding


def deduplicate(findings):
    """Exact fingerprints collapse; so do two claims about one quoted passage
    that share most of their words."""
    kept = []
    for finding in findings:
        duplicate = None
        words = set(normalize_claim(finding.claim).split())
        for other in kept:
            if other.fingerprint == finding.fingerprint:
                duplicate = other
                break
            if (
                other.anchor.page_id == finding.anchor.page_id
                and squash(other.anchor.quote) == squash(finding.anchor.quote)
                and other.category == finding.category
            ):
                shared = words & set(normalize_claim(other.claim).split())
                if len(shared) >= max(3, 0.6 * len(words)):
                    duplicate = other
                    break
        if duplicate is None:
            kept.append(finding)
        elif ORDER[finding.severity_proposed] < ORDER[duplicate.severity_proposed]:
            duplicate.severity_proposed = finding.severity_proposed
    return kept
