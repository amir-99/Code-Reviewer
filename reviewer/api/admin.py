from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from reviewer.accounts.policy import review_access
from reviewer.api.accounts import authenticate
from reviewer.config.loader import load_project
from reviewer.config.models import assignment, catalog, resolve
from reviewer.config.schema import ROLES

router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])


@router.get("/models")
async def models(request: Request, project_id: int | None = None):
    """The roles a review selects a model for, and what an operator may choose.

    `defaults` is what a run uses when the operator names nothing, resolved
    through the same layers a review resolves through, so the dashboard shows
    the model that will actually run rather than a guess. `catalog` is the set
    of IDs a manual run may name; it is operator configuration, not a list
    discovered from the gateway.
    """
    settings = request.app.state.settings
    config = load_project(settings.config_path, project_id or 0)
    return {
        "roles": list(ROLES),
        "defaults": assignment(resolve(settings, config)),
        "catalog": catalog(settings, config),
    }


@router.get("/reviews")
async def lookup(
    request: Request,
    event_id: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    owner: str | None = None,
):
    """Resolve a trigger response's event id to its review.

    The trigger enqueues and returns; the worker creates the review a moment
    later. Until it does there is nothing to report, which is a queued run
    rather than an error.
    """
    if event_id is None:
        account = request.state.principal
        if owner is not None and account.role != "admin":
            raise HTTPException(403, "Admin role required")
        rows = await request.app.state.store.recent(
            limit,
            offset,
            owner_user_id=(None if owner == "system" else owner)
            if account.role == "admin"
            else account.id,
            all_owners=account.role == "admin" and owner is None,
        )
        for row in rows:
            row["spend"] = await request.app.state.store.spend(row["id"])
        return {"reviews": rows}
    from reviewer.store.models import ReviewTrigger

    async with request.app.state.store.sessions() as session:
        trigger = await session.get(ReviewTrigger, event_id)
    account = request.state.principal
    if account.role != "admin" and (
        trigger is None or trigger.owner_user_id != account.id
    ):
        raise HTTPException(404, "Review not found")
    review = await request.app.state.store.by_event(event_id)
    if review is None:
        if trigger is None:
            raise HTTPException(404, "Review not found")
        return {"state": trigger.state, "event_id": event_id, "findings": []}
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
    review = await review_access(request, review_id)
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
    body["owner_user_id"] = review.owner_user_id
    body["capabilities"] = {
        "execute": request.state.principal.role == "user"
        and review.owner_user_id == request.state.principal.id
    }
    body["project_id"] = await store.project_number(review)
    body["overrides"] = review.overrides
    body["findings"] = [summarise(f) for f in await store.findings_for(review_id)]
    snapshot = await store.snapshot(review_id) or {}
    # The run's own record of which model served each role, so a finished review
    # still reports what produced it once its events have scrolled away.
    body["models"] = (snapshot.get("bundle") or {}).get("budget", {}).get(
        "model_tier"
    ) or await store.event_data(review_id, "models")

    body["report"] = snapshot.get("report")
    body["recheck"] = snapshot.get("recheck")
    # What the run has spent so far, against the ceiling it is allowed to spend.
    # Read live from the audited calls, so it is answerable mid-review and not
    # only once a snapshot exists.
    budget = (
        (snapshot.get("bundle") or {}).get("budget")
        or await store.event_data(review_id, "budget")
        or {}
    )
    body["spend"] = await store.spend(review_id) | {
        "token_ceiling": budget.get("token_ceiling")
    }
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
        "impact_level": finding.get("impact_level"),
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
    review = await review_access(request, review_id, execute=True)
    project_id = await request.app.state.store.project_number(review)
    from reviewer.accounts.credentials import Credentials, CredentialUnavailable
    from reviewer.api.webhooks import ReviewJob
    from reviewer.services.forge.gitlab import GitLab
    from reviewer.store.models import ReviewTrigger

    settings = request.app.state.settings
    service = Credentials(request.app.state.store, settings)
    try:
        refs = await service.pin(request.state.principal.id)
        context = await service.load(request.state.principal.id, refs)
    except CredentialUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    forge = getattr(request.app.state, "personal_forge_factory", GitLab)(
        settings.gitlab_base_url, context.tokens["gitlab"]
    )
    try:
        principal_id = str(await forge.identity())
        mr = await forge.get_merge_request(project_id, review.mr_iid)
        if mr.state != "opened" or mr.draft:
            raise HTTPException(409, "Merge request is closed, merged or a draft")
    finally:
        await forge.close()
    job = ReviewJob(
        project_id=project_id,
        iid=review.mr_iid,
        head_sha=mr.head_sha,
        event_id=str(uuid4()),
        overrides=review.overrides,
    )
    async with request.app.state.store.transaction() as session:
        session.add(
            ReviewTrigger(
                event_id=job.event_id,
                owner_user_id=request.state.principal.id,
                credential_refs=refs,
                payload=job.model_dump() | {"principal_id": principal_id},
            )
        )
    await request.app.state.queue.enqueue_job(
        "receive_event", job.model_dump(), _job_id=job.event_id
    )
    return {"accepted": True, "event_id": job.event_id}


@router.post("/reviews/{review_id}/recheck")
async def recheck(review_id: str, request: Request):
    """Re-judge the comments this review published, at the branch's current head.

    Unlike a replay this runs no stages and publishes no report: it only answers
    the threads that are already open.
    """
    review = await review_access(request, review_id, execute=True)
    project_id = await request.app.state.store.project_number(review)
    await request.app.state.queue.enqueue_job(
        "recheck_review", project_id, review.mr_iid, str(review.id)
    )
    return {"accepted": True, "project_id": project_id, "iid": review.mr_iid}


@router.get("/quality")
async def quality(request: Request, project_id: int | None = None):
    from reviewer.telemetry.quality import quality as query

    return await query(
        request.app.state.store,
        project_id,
        owner_user_id=request.state.principal.id,
        all_owners=request.state.principal.role == "admin",
    )


@router.get("/reviews/{review_id}/audit")
async def audit(review_id: str, request: Request):
    await review_access(request, review_id)
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
                if column.name not in {"prompt_blob_ref", "response_blob_ref"}
            }
            for row in rows
        ]
