"""Central review access rules, independent of dashboard visibility."""

from fastapi import HTTPException


async def review_access(request, review_id, execute=False):
    account = request.state.principal
    review = await request.app.state.store.get(review_id)
    if review is None or (
        account.role != "admin" and review.owner_user_id != account.id
    ):
        raise HTTPException(404, "Review not found")
    if execute and (account.role != "user" or review.owner_user_id != account.id):
        raise HTTPException(403, "Review execution is restricted to its owner")
    return review


# A failed run can be repeated as it was. A run that finished, was halted or
# superseded is run again at the current head instead.
RETRYABLE = frozenset({"FAILED_CONTEXT", "FAILED_INTERNAL"})


def run_controls(account, owner_user_id, state, *, overtaken=False):
    """Which ways of running a review again `account` may use, and why not.

    `rerun` starts a fresh review at the merge request's current head (or the
    page's current version) with the context the review was triggered with; on
    a review still in flight it supersedes that run, as a manual trigger does.
    `retry` repeats a failed run on the same head with the same context, and is
    refused once anything has run after it. Both stay owner-only: admins read
    every review but never execute one. The head is compared when a retry is
    requested, not here, so this stays a pure reading of stored state.
    """
    from reviewer.orchestrator.states import TERMINAL

    owner = account.role == "user" and owner_user_id == account.id
    if not owner:
        refused = {
            "allowed": False,
            "reason": "Only the review's owner can run it again",
        }
        return {"rerun": refused, "retry": refused}
    rerun = {"allowed": True, "reason": None}
    if state not in RETRYABLE:
        reason = (
            "The review is still running"
            if state not in TERMINAL
            else "Only a failed review can be retried"
        )
        retry = {"allowed": False, "reason": reason}
    elif overtaken:
        retry = {
            "allowed": False,
            "reason": "A later review has run since this one; rerun instead",
        }
    else:
        retry = {"allowed": True, "reason": None}
    return {"rerun": rerun, "retry": retry}
