import hmac
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request


async def authenticate(request: Request):
    secret = request.app.state.settings.admin_token.get_secret_value()
    supplied = request.headers.get("authorization", "")
    if not secret or not hmac.compare_digest(
        supplied.encode(), f"Bearer {secret}".encode()
    ):
        raise HTTPException(401, "Invalid admin token")


router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])


@router.get("/reviews")
async def lookup(
    request: Request,
    event_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Resolve a trigger response's event id to its review.

    The trigger enqueues and returns; the worker creates the review a moment
    later. Until it does there is nothing to report, which is a queued run
    rather than an error.
    """
    if event_id is None:
        return {"reviews": await request.app.state.store.recent(limit, offset)}
    review = await request.app.state.store.by_event(event_id)
    if review is None:
        return {"state": "QUEUED", "event_id": event_id, "findings": []}
    return await inspect(review.id, request)


@router.get("/reviews/{review_id}")
async def inspect(review_id: str, request: Request):
    """Review progress plus, once analysis has run, its findings.

    The trigger endpoint answers before any work happens, so this is where a
    manual run reads its results back. `report` carries the rendered comment,
    and `recheck` the answers the last recheck gave this review's open threads.
    A drafted run's comments wait on the merge request as GitLab draft notes,
    so both are the record of what was queued rather than posted.
    """
    store = request.app.state.store
    review = await store.get(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    body = {
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
    body["project_id"] = await store.project_number(review)
    body["overrides"] = review.overrides
    body["findings"] = [summarise(f) for f in await store.findings_for(review_id)]
    snapshot = await store.snapshot(review_id) or {}
    body["report"] = snapshot.get("report")
    body["recheck"] = snapshot.get("recheck")
    return body


def summarise(finding):
    """The reviewer-facing shape of a stored finding.

    Provenance, prompts and raw model text stay in the audit endpoint; this is
    the operator's view of what the review actually concluded.
    """
    anchor = finding.get("anchor") or {}
    verification = finding.get("verification") or {}
    return {
        "id": finding.get("id"),
        "fingerprint": finding.get("fingerprint"),
        "stage": finding.get("stage"),
        "category": finding.get("category"),
        "severity": finding.get("severity_final"),
        "severity_proposed": finding.get("severity_proposed"),
        "status": finding.get("status"),
        "confidence": finding.get("confidence"),
        "claim": finding.get("claim"),
        "reason": finding.get("reason"),
        "impact": finding.get("impact"),
        "failure_scenario": finding.get("failure_scenario"),
        "suggested_direction": finding.get("suggested_direction"),
        "requirement_ref": finding.get("requirement_ref"),
        "file": anchor.get("file"),
        "line_start": anchor.get("line_start"),
        "line_end": anchor.get("line_end"),
        "introduced_by_this_change": anchor.get("introduced_by_this_change"),
        "evidence": finding.get("evidence") or [],
        "verdict": verification.get("verdict"),
        "resolution": finding.get("resolution"),
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


@router.post("/reviews/{review_id}/recheck")
async def recheck(review_id: str, request: Request):
    """Re-judge the comments this review published, at the branch's current head.

    Unlike a replay this runs no stages and publishes no report: it only answers
    the threads that are already open.
    """
    review = await request.app.state.store.get(review_id)
    if review is None:
        raise HTTPException(404, "Review not found")
    project_id = await request.app.state.store.project_number(review)
    await request.app.state.queue.enqueue_job(
        "recheck_review", project_id, review.mr_iid
    )
    return {"accepted": True, "project_id": project_id, "iid": review.mr_iid}


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
