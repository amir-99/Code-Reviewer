from enum import StrEnum

from reviewer.findings.models import Severity


class Decision(StrEnum):
    COMMENT_ONLY = "COMMENT_ONLY"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE = "APPROVE"


def decide(review, findings, static_results, config):
    if review.partial or not review.complete:
        return Decision.COMMENT_ONLY
    if any(r.required and r.status == "failed" for r in static_results):
        return Decision.REQUEST_CHANGES
    if any(
        f.status not in {"discarded", "suppressed", "resolved"}
        and f.verified
        and (
            f.severity_final == Severity.BLOCKER
            or (
                f.severity_final == Severity.REQUIRED
                and f.anchor.introduced_by_this_change
            )
        )
        for f in findings
    ):
        return Decision.REQUEST_CHANGES
    return Decision.APPROVE


def status(decision, mode, failed=False):
    return (
        "failed"
        if not failed and mode == "gating" and decision == Decision.REQUEST_CHANGES
        else "success"
    )
