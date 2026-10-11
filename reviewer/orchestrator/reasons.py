"""Why a review failed, stopped short or ran partial, in words a reader can act on.

The pipeline records degradation codes and a failure reason; this turns them
into sentences. Reasons are written from what code knows, never from exception
text: an upstream failure may carry credentials or private payloads, so a
failure is named by its kind and the stage it happened in.
"""

# Exception type names, imported lazily by callers, and what each one means.
FAILURES = {
    "GitError": "The repository could not be fetched or checked out",
    "ScanError": "Secret scanning could not run, so no code was sent for review",
    "FileNotFoundError": "A file or tool the review needed was missing",
    "BudgetExhausted": "The review's token budget ran out",
    "StageFailed": "A review stage failed after its retries",
    "TimeoutError": "The review ran out of time",
    "CredentialUnavailable": "The owner's account or pinned credentials are unavailable",
}


# A review fails in INIT when its workspace cannot even be prepared.
STAGES = {"INIT": "setup"}


def failure_reason(exc, state):
    """The reason recorded on a review that failed in `state` with `exc`."""
    kind = type(exc).__name__
    message = FAILURES.get(kind, "An internal error stopped the review")
    stage = STAGES.get(str(state), str(state).replace("_", " ").lower())
    return f"{message} during {stage} ({kind})"


# Degradation codes that make a review partial, and what each one means.
PARTIAL = {
    "budget_exhausted": "The token budget ran out before every unit was examined",
    "prompt_requirements_truncated": "Requirements were too long for the prompt "
    "and were truncated",
    "unverified_scope_claims": "Some findings that reach beyond the changed code "
    "could not be verified",
    "gateway_unconfigured": "No model gateway is configured, so no AI analysis ran",
}
# Codes that limit what the review could see without making it partial.
DEGRADED = {
    "static_unavailable": "A static analyzer errored or was skipped",
    "build_unavailable": "The sandboxed build could not run",
    "build_timeout": "The sandboxed build timed out",
    "build_agent_unavailable": "The build agent failed; only the baseline build ran",
    "jira_unavailable": "Jira could not be reached, so requirements are missing",
    "docs_unavailable": "Linked Confluence pages could not be read",
    "docs_truncated": "Linked pages were cut to fit the document budget",
    "requirements_condensed": "Requirements were condensed to fit the prompt",
    "unlinked": "No Jira story is linked to this change",
    "context_plan_unavailable": "The context planner failed; units ran without "
    "planned context",
    "severity_triage_unavailable": "Severity triage was unavailable; severities "
    "are as proposed",
    "scope_unavailable": "Code outside the change could not be searched",
}


def degradation(code):
    """One reason for a degradation code, or None for a code with no reader value."""
    if code == "partial" or code.startswith("excluded:"):
        return None  # The flag itself, and per-file exclusions counted separately.
    if code.startswith("failed_stage:"):
        stage = code.split(":", 1)[1].replace("_", " ")
        return {"code": code, "partial": True, "message": f"The {stage} stage failed"}
    if code.startswith("partial:"):
        stage = code.split(":", 1)[1].replace("_", " ")
        return {
            "code": code,
            "partial": True,
            "message": f"The {stage} stage did not cover every unit",
        }
    if code in PARTIAL:
        return {"code": code, "partial": True, "message": PARTIAL[code]}
    if code in DEGRADED:
        return {"code": code, "partial": False, "message": DEGRADED[code]}
    return {"code": code, "partial": False, "message": code.replace("_", " ")}


def reasons(review, degradations=()):
    """Every reason a reader should see for how `review` ended.

    The terminal reason comes first, then what made the review partial, then
    what limited it. A partial review with no recorded cause says so rather
    than inventing one.
    """
    found = []
    state = review.state
    if review.error:
        found.append({"code": "error", "partial": False, "message": review.error})
    elif state == "SUPERSEDED":
        found.append(
            {
                "code": "superseded",
                "partial": False,
                "message": "A newer review of this change replaced this one",
            }
        )
    elif state == "CANCELLED":
        found.append(
            {
                "code": "cancelled",
                "partial": False,
                "message": "The merge request was closed, merged or made a draft",
            }
        )
    elif state in {"FAILED_INTERNAL", "FAILED_CONTEXT"}:
        found.append(
            {
                "code": "failed",
                "partial": False,
                "message": "The review failed; no reason was recorded",
            }
        )
    seen = set()
    for code in degradations or ():
        item = degradation(str(code))
        if item and item["code"] not in seen:
            seen.add(item["code"])
            found.append(item)
    excluded = sum(
        1 for code in degradations or () if str(code).startswith("excluded:")
    )
    if excluded:
        found.append(
            {
                "code": "excluded",
                "partial": False,
                "message": f"{excluded} changed file(s) were excluded from review",
            }
        )
    if (
        review.partial
        and state == "PUBLISHED"
        and not any(item["partial"] for item in found)
    ):
        found.append(
            {
                "code": "partial",
                "partial": True,
                "message": "Coverage was incomplete; this installation's "
                "milestone or configuration limits the analysis",
            }
        )
    return found
