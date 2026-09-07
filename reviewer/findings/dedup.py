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


def deduplicate(findings, distance=10):
    exact = {}
    for f in findings:
        if f.fingerprint in exact:
            combine(exact[f.fingerprint], f)
        else:
            exact[f.fingerprint] = f
    merged = []
    for f in exact.values():
        related = next(
            (
                x
                for x in merged
                if x.anchor.file == f.anchor.file
                and x.category == f.category
                and abs(x.anchor.line_start - f.anchor.line_start) <= distance
            ),
            None,
        )
        if related:
            combine(related, f)
        else:
            merged.append(f)
    return merged
