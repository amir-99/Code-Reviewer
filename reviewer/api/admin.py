import hmac
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request


async def authenticate(request: Request):
    secret = request.app.state.settings.admin_token.get_secret_value()
    supplied = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(
        supplied.encode(), f"Bearer {secret}".encode()
    ):
        raise HTTPException(401, "Invalid admin token")


router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])


@router.get("/reviews/{review_id}")
async def inspect(review_id: str, request: Request):
    review = await request.app.state.store.get(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    return {
        key: getattr(review, key)
        for key in (
            "id",
            "mr_iid",
            "head_sha",
            "state",
            "decision",
            "partial",
            "history",
            "started_at",
            "finished_at",
            "status_delivered",
            "error",
        )
    }


@router.post("/reviews/{review_id}/replay")
async def replay(review_id: str, request: Request):
    review = await request.app.state.store.get(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    project_id = await request.app.state.store.project_number(review)
    await request.app.state.queue.enqueue_job(
        "replay_review", project_id, review.mr_iid, str(uuid4()), review.overrides
    )
    return {"accepted": True}


@router.get("/quality")
async def quality(request: Request, project_id: int | None = None):
    from reviewer.telemetry.quality import quality as query

    return await query(request.app.state.store, project_id)


@router.get("/reviews/{review_id}/audit")
async def audit(review_id: str, request: Request):
    from sqlalchemy import select

    from reviewer.store.models import LLMCall

    async with request.app.state.store.sessions() as session:
        rows = (
            await session.scalars(select(LLMCall).where(LLMCall.review_id == review_id))
        ).all()
        return [
            {
                column.name: getattr(row, column.name)
                for column in LLMCall.__table__.columns
            }
            for row in rows
        ]
