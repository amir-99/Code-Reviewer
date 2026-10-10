import hashlib
import posixpath
import re
import unicodedata

from reviewer.findings.models import Severity

ORDER = {s: i for i, s in enumerate(Severity)}


def normalize_claim(claim):
    claim = "".join(
        c
        for c in claim.lower()
        if not unicodedata.category(c).startswith("P") and not c.isdigit()
    )
    return re.sub(r"\s+", " ", claim).strip()


def fingerprint(project_id, path, category, claim, symbol=None):
    normalized_path = posixpath.normpath(path.replace("\\", "/"))
    return hashlib.sha256(
        f"{project_id}|{normalized_path}|{category}|{normalize_claim(claim)}|{symbol or ''}".encode()
    ).hexdigest()[:32]


def combine(first, other):
    # Keep impact_level with the retained claim, impact prose and provenance.
    # Never take the maximum from a nearby claim describing another risk.
    if ORDER[other.severity_proposed] < ORDER[first.severity_proposed]:
        first.severity_proposed = other.severity_proposed
    seen = {(e.file, e.line_start, e.line_end, e.note) for e in first.evidence}
    first.evidence.extend(
        e
        for e in other.evidence
        if (e.file, e.line_start, e.line_end, e.note) not in seen
    )
    first.stage = ",".join(
        sorted(set(first.stage.split(",")) | set(other.stage.split(",")))
    )
    return first


def similar(first, other, threshold=0.5):
    """Word overlap of two normalized claims (Jaccard), for near-duplicates."""
    a, b = set(normalize_claim(first).split()), set(normalize_claim(other).split())
    return bool(a and b) and len(a & b) / len(a | b) >= threshold


def related(first, other, distance):
    """Nearby claims merge only when they describe the same defect.

    Proximity alone is not identity: two distinct defects of one category can
    sit a few lines apart in one function. Merge when the cited ranges overlap,
    or when nearby claims are worded alike.
    """
    if first.anchor.file != other.anchor.file or first.category != other.category:
        return False
    if abs(first.anchor.line_start - other.anchor.line_start) > distance:
        return False
    overlap = (
        first.anchor.line_start <= other.anchor.line_end
        and other.anchor.line_start <= first.anchor.line_end
    )
    return overlap or similar(first.claim, other.claim)


def deduplicate(findings, distance=10):
    exact = {}
    for f in findings:
        if f.fingerprint in exact:
            combine(exact[f.fingerprint], f)
        else:
            exact[f.fingerprint] = f
    merged = []
    for f in exact.values():
        related_finding = next(
            (x for x in merged if related(x, f, distance)),
            None,
        )
        if related_finding:
            combine(related_finding, f)
        else:
            merged.append(f)
    return merged
