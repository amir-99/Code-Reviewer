from reviewer.findings.models import Confidence, Severity

BASE = {
    "security": Severity.BLOCKER,
    "data_integrity": Severity.BLOCKER,
    "prompt_injection": Severity.BLOCKER,
    "correctness": Severity.REQUIRED,
    "concurrency": Severity.REQUIRED,
    "error_handling": Severity.REQUIRED,
    "test_gap": Severity.REQUIRED,
    "maintainability": Severity.SUGGESTION,
    "complexity": Severity.SUGGESTION,
    "naming": Severity.NIT,
    "style": Severity.NIT,
}


def needs_verification(f):
    return BASE.get(f.category, Severity.SUGGESTION) in {
        Severity.BLOCKER,
        Severity.REQUIRED,
    } or f.severity_proposed in {Severity.BLOCKER, Severity.REQUIRED}


def normalize(f, touched, static_categories=()):
    if f.status in {"discarded", "suppressed", "resolved"}:
        return f
    severity = BASE.get(f.category, Severity.SUGGESTION)
    if (
        (needs_verification(f) and not f.verified)
        or f.confidence == Confidence.LOW
        or (f.validation and not f.validation.evidence_valid)
    ):
        if severity in {Severity.BLOCKER, Severity.REQUIRED}:
            severity = Severity.SUGGESTION
    if not f.anchor.introduced_by_this_change:
        severity = Severity.FYI
    if f.category in static_categories or (
        not f.anchor.in_diff and f.anchor.file not in touched
    ):
        f.status = "suppressed"
    f.severity_final = severity
    return f
