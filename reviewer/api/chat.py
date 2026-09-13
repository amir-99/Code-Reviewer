"""Questions about a finished review: owner asks, admin reads, the worker answers."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from reviewer.accounts.credentials import Credentials, CredentialUnavailable
from reviewer.accounts.policy import review_access
from reviewer.api.accounts import Input, authenticate
from reviewer.config.loader import load_project
from reviewer.orchestrator.states import TERMINAL

router = APIRouter(prefix="/admin/reviews", dependencies=[Depends(authenticate)])


class Question(Input):
    question: str = Field(min_length=1, max_length=4000)


async def eligibility(request, review):
    """Why this account cannot ask right now, or None when it can.

    Chat reads the stored record, so a review has to have finished and left
    one; it spends the owner's own gateway credential, so only the owner asks.
    """
    store, settings = request.app.state.store, request.app.state.settings
    account = request.state.principal
    if account.role != "user" or review.owner_user_id != account.id:
        return "Only the review's owner can ask about it"
    project = await store.project_number(review)
    if not load_project(settings.config_path, project).chat.enabled:
        return "Chat is disabled for this project"
    if review.state not in TERMINAL or not await store.snapshot(review.id):
        return "The review has not finished yet"
    return None


async def view(request, review, reason):
    store = request.app.state.store
    return {
        "messages": await store.chat_messages(review.id),
        "can_ask": reason is None,
        "reason": reason,
        "spend": (await store.spend(review.id))["chat"],
    }


@router.get("/{review_id}/chat")
async def history(review_id: str, request: Request):
    review = await review_access(request, review_id)
    return await view(request, review, await eligibility(request, review))


@router.post("/{review_id}/chat")
async def ask(review_id: str, body: Question, request: Request):
    review = await review_access(request, review_id, execute=True)
    reason = await eligibility(request, review)
    if reason:
        raise HTTPException(409, reason)
    store, settings = request.app.state.store, request.app.state.settings
    if not body.question.strip():
        raise HTTPException(422, "A question is required")
    project = await store.project_number(review)
    limit = load_project(settings.config_path, project).chat.messages_per_hour
    account = request.state.principal
    if (
        await store.chat_recent(account.id, datetime.now(UTC) - timedelta(hours=1))
        >= limit
    ):
        raise HTTPException(429, "Too many questions this hour")
    # Pin now, not at review time: a credential rotated since the run should
    # answer a question asked today, and a revoked one should refuse it.
    try:
        refs = await Credentials(store, settings).pin(account.id)
    except CredentialUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    message = await store.add_chat_message(
        review.id, account.id, body.question.strip(), refs
    )
    try:
        await request.app.state.queue.enqueue_job(
            "answer_chat", message["id"], _job_id=f"chat:{message['id']}"
        )
    except Exception:
        pass  # The durable pending row is recovered by the worker sweep.
    return {"accepted": True, "message": message}
