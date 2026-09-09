from enum import StrEnum


class ReviewState(StrEnum):
    INIT = "INIT"
    CONTEXT_COLLECTION = "CONTEXT_COLLECTION"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    PURPOSE_REVIEW = "PURPOSE_REVIEW"
    DESIGN_REVIEW = "DESIGN_REVIEW"
    ANALYSIS_FAN_OUT = "ANALYSIS_FAN_OUT"
    SYSTEM_CONTEXT_REVIEW = "SYSTEM_CONTEXT_REVIEW"
    EVIDENCE_VALIDATION = "EVIDENCE_VALIDATION"
    FINDING_VERIFICATION = "FINDING_VERIFICATION"
    FINALIZATION = "FINALIZATION"
    DECISION = "DECISION"
    PUBLISHED = "PUBLISHED"
    TERMINATED_EARLY = "TERMINATED_EARLY"
    FAILED_CONTEXT = "FAILED_CONTEXT"
    FAILED_INTERNAL = "FAILED_INTERNAL"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


TERMINAL = frozenset(
    {
        ReviewState.PUBLISHED,
        ReviewState.TERMINATED_EARLY,
        ReviewState.FAILED_CONTEXT,
        ReviewState.FAILED_INTERNAL,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
    }
)
LEGAL = {
    ReviewState.INIT: {ReviewState.CONTEXT_COLLECTION, ReviewState.FINALIZATION},
    ReviewState.CONTEXT_COLLECTION: {ReviewState.STATIC_ANALYSIS},
    ReviewState.STATIC_ANALYSIS: {ReviewState.PURPOSE_REVIEW},
    ReviewState.PURPOSE_REVIEW: {
        ReviewState.DESIGN_REVIEW,
    },
    ReviewState.DESIGN_REVIEW: {
        ReviewState.ANALYSIS_FAN_OUT,
    },
    ReviewState.ANALYSIS_FAN_OUT: {ReviewState.SYSTEM_CONTEXT_REVIEW},
    ReviewState.SYSTEM_CONTEXT_REVIEW: {ReviewState.EVIDENCE_VALIDATION},
    ReviewState.EVIDENCE_VALIDATION: {ReviewState.FINDING_VERIFICATION},
    ReviewState.FINDING_VERIFICATION: {ReviewState.FINALIZATION},
    ReviewState.FINALIZATION: {ReviewState.DECISION},
    ReviewState.DECISION: {ReviewState.PUBLISHED},
}
for state in set(ReviewState) - TERMINAL:
    LEGAL[state] |= {
        ReviewState.FAILED_INTERNAL,
        ReviewState.FAILED_CONTEXT,
        ReviewState.CANCELLED,
        ReviewState.SUPERSEDED,
    }


def check_transition(old: str, new: str) -> None:
    if ReviewState(new) not in LEGAL.get(ReviewState(old), set()):
        raise ValueError(f"Illegal review transition: {old} -> {new}")


# Short circuits for feature milestones.
for stage in (
    "CONTEXT_COLLECTION",
    "STATIC_ANALYSIS",
    "PURPOSE_REVIEW",
    "DESIGN_REVIEW",
):
    LEGAL[ReviewState(stage)].add(ReviewState.FINALIZATION)
