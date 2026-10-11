from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from reviewer.accounts.policy import review_access, run_controls
from reviewer.api.accounts import authenticate
from reviewer.config.loader import load_project
from reviewer.config.models import assignment, catalog, resolve
from reviewer.config.schema import ROLES
from reviewer.store.repositories import subject_view

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
        overtaken = await request.app.state.store.overtaken([r["id"] for r in rows])
        for row in rows:
            row["spend"] = await request.app.state.store.spend(row["id"])
            row["controls"] = run_controls(
                account,
                row["owner_user_id"],
                row["state"],
                overtaken=row["id"] in overtaken,
            )
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
    body["kind"] = review.kind
    body["subject"] = subject_view(review.subject)
    body["missing_integrations"] = (
        sorted({"jira", "confluence"} - (review.credential_refs or {}).keys())
        if review.owner_user_id and review.kind != "document"
        else []
    )
    owner = (
        request.state.principal.role == "user"
        and review.owner_user_id == request.state.principal.id
    )
    body["capabilities"] = {"execute": owner, "chat": owner}
    # Rerun and retry, each with whether the API would accept it now and why
    # not. The dashboard draws its buttons from this rather than its own rules.
    body["controls"] = run_controls(
        request.state.principal,
        review.owner_user_id,
        review.state,
        overtaken=review.id in await store.overtaken([review.id]),
    )
    body["project_id"] = await store.project_number(review)
    body.update(await store.project_identity(review))
    body["overrides"] = review.overrides
    body["findings"] = [summarise(f) for f in await store.findings_for(review_id)]
    snapshot = await store.snapshot(review_id) or {}
    from reviewer.api.review_links import review_links

    body["links"] = review_links(snapshot, review.overrides, request.app.state.settings)
    # The run's own record of which model served each role, so a finished review
    # still reports what produced it once its events have scrolled away.
    body["models"] = (snapshot.get("bundle") or {}).get("budget", {}).get(
        "model_tier"
    ) or await store.event_data(review_id, "models")

    # Why the review failed, stopped or ran partial, as sentences: the stored
    # failure reason first, then the run's own degradation codes.
    from reviewer.orchestrator.reasons import reasons

    body["reasons"] = reasons(
        review, (snapshot.get("bundle") or {}).get("degradations") or ()
    )
    # Static analyzers and build commands as the run finished them. While it
    # runs, their output streams as `check` activity instead.
    from reviewer.telemetry.checks import summary

    body["checks"] = [
        summary(result) for result in (snapshot.get("bundle") or {}).get("static") or []
    ]
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
        # Document findings anchor on a quoted passage of a page section.
        "page_id": anchor.get("page_id"),
        "heading_path": anchor.get("heading_path"),
        "quote": anchor.get("quote"),
        "related": finding.get("related") or [],
        "evidence": finding.get("evidence") or [],
        "verdict": verification.get("verdict"),
        "unverified_reason": finding.get("unverified_reason"),
        "resolution": finding.get("resolution"),
    }


@router.post("/reviews/{review_id}/stop")
async def stop(review_id: str, request: Request):
    """Stop a review in flight.

    The cancellation is committed here; the worker running the review sees it
    within seconds, cancels the run and delivers a passing commit status, as
    for any cancelled review. Comments already posted stay posted.
    """
    review = await review_access(request, review_id, execute=True)
    store = request.app.state.store
    control = run_controls(request.state.principal, review.owner_user_id, review.state)[
        "stop"
    ]
    if not control["allowed"]:
        raise HTTPException(409, control["reason"])
    if await store.stop(review.id) is None:
        raise HTTPException(409, "The review moved on before it could be stopped")
    await store.append_event(
        review.id,
        "request",
        {
            "name": f"Stop requested by {request.state.principal.id}",
            "status": "completed",
        },
    )
    return {"stopped": True, "state": "CANCELLED"}


@router.post("/reviews/{review_id}/rerun")
@router.post("/reviews/{review_id}/replay")  # The original name, kept for clients.
async def rerun(review_id: str, request: Request):
    """A fresh review at the current head, with the context this one was given."""
    return await run_again(review_id, request, retry=False)


@router.post("/reviews/{review_id}/retry")
async def retry(review_id: str, request: Request):
    """Repeat a failed review on the head it failed on, with the same context.

    Refused once the merge request has new commits (or the page a new version):
    what is there now is a rerun, not the failed attempt.
    """
    return await run_again(review_id, request, retry=True)


async def run_again(review_id, request, *, retry):
    review = await review_access(request, review_id, execute=True)
    store = request.app.state.store
    control = run_controls(
        request.state.principal,
        review.owner_user_id,
        review.state,
        overtaken=review.id in await store.overtaken([review.id]),
    )["retry" if retry else "rerun"]
    if not control["allowed"]:
        raise HTTPException(409, control["reason"])
    if review.kind == "document":
        from reviewer.api.document_reviews import replay_document

        result = await replay_document(review, request, retry=retry)
    else:
        result = await replay_code(review, request, retry=retry)
    mode = "retry" if retry else "rerun"
    await store.append_event(
        review.id,
        "request",
        {
            "name": f"{mode.capitalize()} requested by {request.state.principal.id}",
            "status": "completed",
            "event_id": result["event_id"],
        },
    )
    return result | {
        "mode": mode,
        "poll": f"/admin/reviews?event_id={result['event_id']}",
    }


async def replay_code(review, request, *, retry):
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
    import httpx

    forge = getattr(request.app.state, "personal_forge_factory", GitLab)(
        settings.gitlab_base_url, context.tokens["gitlab"]
    )
    try:
        principal_id = str(await forge.identity())
        mr = await forge.get_merge_request(project_id, review.mr_iid)
        if mr.state != "opened" or mr.draft:
            raise HTTPException(409, "Merge request is closed, merged or a draft")
    except httpx.HTTPError:
        raise HTTPException(502, "GitLab lookup is unavailable") from None
    finally:
        await forge.close()
    if retry and mr.head_sha != review.head_sha:
        raise HTTPException(
            409,
            "The merge request has new commits since this review; rerun it instead",
        )
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
    try:
        await request.app.state.queue.enqueue_job(
            "receive_event", job.model_dump(), _job_id=job.event_id
        )
    except Exception:
        pass  # The durable trigger is recovered independently of this request.
    return {"accepted": True, "event_id": job.event_id}


@router.post("/reviews/{review_id}/recheck")
async def recheck(review_id: str, request: Request):
    """Re-judge the comments this review published, at the branch's current head.

    Unlike a replay this runs no stages and publishes no report: it only answers
    the threads that are already open.
    """
    review = await review_access(request, review_id, execute=True)
    if review.kind == "document":
        raise HTTPException(409, "Document reviews have no threads to recheck")
    project_id = await request.app.state.store.project_number(review)
    from reviewer.telemetry.activity import queue_recheck

    # Before the job, so the stream a reader reconnects holds open for it.
    await queue_recheck(request.app.state.store, review.id)
    try:
        await request.app.state.queue.enqueue_job(
            "recheck_review", project_id, review.mr_iid, str(review.id)
        )
    except Exception:
        await queue_recheck(request.app.state.store, review.id, "failed")
        raise
    await request.app.state.store.append_event(
        review.id,
        "request",
        {
            "name": f"Recheck requested by {request.state.principal.id}",
            "status": "completed",
        },
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
