"""Authenticated durable SSE. A database read per second crosses worker processes."""

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from reviewer.api.admin import authenticate, inspect
from reviewer.orchestrator.states import TERMINAL

router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])


def frame(event, data, sequence=None):
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return f"{prefix}event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream(request, review_id, after):
    store = request.app.state.store
    yield "retry: 2000\n\n"
    yield frame("snapshot", await inspect(review_id, request))
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
        if review.state in TERMINAL:
            yield frame("snapshot", await inspect(review_id, request))
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
