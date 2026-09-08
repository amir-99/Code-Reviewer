"""Authenticated durable SSE. A database read per second crosses worker processes."""

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from reviewer.api.admin import authenticate, inspect
from reviewer.orchestrator.states import TERMINAL

router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])

# A worker that dies between the terminal transition and its closing marker
# would otherwise hold the stream open for ever.
CLOSING_GRACE = 30.0


def frame(event, data, sequence=None):
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def picture(request, review_id):
    """A snapshot frame that says which events it already reflects.

    The cursor is read first, so an event committed while the body is being
    assembled is above it and gets applied by the client rather than dropped.
    """
    sequence = await request.app.state.store.last_sequence(review_id)
    return frame("snapshot", dict(await inspect(review_id, request), sequence=sequence))


async def closing(store, review):
    """Whether a terminal review's worker is still finishing its run.

    The terminal transition is committed before the commit status is delivered
    and before the worktree is torn down, and both still write activity. Ending
    the stream on the transition alone reports the review finished while its own
    run is still going, and freezes the snapshot on an undelivered status.
    """
    if await store.run_state(review.id) != "started":
        return False
    finished = getattr(review, "finished_at", None)
    if finished is None:
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=UTC)
    return (datetime.now(UTC) - finished).total_seconds() < CLOSING_GRACE


async def stream(request, review_id, after):
    store = request.app.state.store
    yield "retry: 2000\n\n"
    yield await picture(request, review_id)
    ticks = 0
    while not await request.is_disconnected():
        # Read terminal state first, then drain events committed with that state.
        review = await store.get(review_id)
        rows = await store.events(review_id, after)
        for row in rows:
            after = row["id"]
            yield frame("activity", row, after)
        if len(rows) == 200:
            continue
        if review.state in TERMINAL and not await closing(store, review):
            # Read last, so the closing snapshot carries the delivered status.
            yield await picture(request, review_id)
            yield frame("complete", {"state": review.state})
            return
        ticks += 1
        if ticks % 15 == 0:
            yield ": heartbeat\n\n"
        await asyncio.sleep(1)


@router.get("/reviews/{review_id}/events")
async def events(
    request: Request, review_id: str, last_event_id: str = Header(default="0")
):
    try:
        after = int(last_event_id)
        if not 0 <= after <= 2**63 - 1:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None
    if await request.app.state.store.get(review_id) is None:
        raise HTTPException(404, "Review not found")
    return StreamingResponse(
        stream(request, review_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
