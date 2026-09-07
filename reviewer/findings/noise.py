from collections import Counter

from reviewer.findings.dedup import ORDER
from reviewer.findings.models import Severity


def select(findings, max_inline=15, max_per_file=5):
    inline = []
    summary = []
    overflow = Counter()
    per_file = Counter()
    existing = 0
    candidates = sorted(
        (
            f
            for f in findings
            if f.status not in {"discarded", "suppressed", "resolved"}
        ),
        key=lambda f: (
            ORDER[f.severity_final],
            not f.anchor.introduced_by_this_change,
            {"high": 0, "medium": 1, "low": 2}[f.confidence],
            -len(f.evidence),
        ),
    )
    for f in candidates:
        if not f.anchor.introduced_by_this_change:
            if existing >= 2:
                overflow[f.category] += 1
                continue
            existing += 1
        if (
            f.severity_final
            in {Severity.NIT, Severity.FYI, Severity.QUESTION, Severity.PRAISE}
            or not f.anchor.in_diff
        ):
            summary.append(f)
        elif len(inline) >= min(15, max_inline) or per_file[f.anchor.file] >= min(
            5, max_per_file
        ):
            overflow[f.category] += 1
        else:
            inline.append(f)
            per_file[f.anchor.file] += 1
    return inline, summary, dict(overflow)
