"""Reviews of Confluence pages, triggered by a person pasting a page link.

A document review is the merge-request review's sibling: the same account,
credential and queue path, but the subject is a page at a version rather than
a branch at a commit. The API resolves the page with the caller's own
Confluence token, records what was asked for, and enqueues. Admission happens
in the worker, exactly as it does for a merge request.
"""

import asyncio
import hashlib
from typing import Literal
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from reviewer.api.accounts import user
from reviewer.api.manual import check_document_urls, check_models
from reviewer.context.models import ReportMode, ReviewOverrides

logger = structlog.get_logger()
router = APIRouter(prefix="/admin", dependencies=[Depends(user)])

DOCUMENT_ROLES = ("document_review", "document_verification", "chat")


class DocumentSubject(BaseModel):
    """The page a review is about, pinned to the version it read."""

    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(pattern=r"^\d{1,20}$")
    space: str = Field(max_length=255)
    title: str = Field(max_length=1000)
    url: str = Field(max_length=2048)
    version: int = Field(ge=1)


class DocumentJob(BaseModel):
    """What the queue carries for a document review; never an MR job."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["document"] = "document"
    event_id: str = Field(max_length=128)
    subject: DocumentSubject
    overrides: ReviewOverrides
    principal_id: str = Field(default="", max_length=128)


class DocumentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_url: str = Field(max_length=2048)
    supporting_urls: list[str] = Field(default_factory=list, max_length=20)
    instruction: str | None = Field(default=None, max_length=4000)
    # Search the page's own space for related pages and check consistency.
    check_space: bool = False
    # "applied" posts page comments, "draft" stores them for the owner to
    # publish from the dashboard, "none" stores the report only.
    report_mode: ReportMode = "applied"
    models: dict[str, str] = Field(default_factory=dict, max_length=len(DOCUMENT_ROLES))


@router.post("/document-reviews", status_code=202)
async def trigger(body: DocumentReviewRequest, request: Request):
    settings = request.app.state.settings
    if not settings.confluence_base_url:
        raise HTTPException(400, "Confluence is not configured on this installation")
    check_document_urls(
        [body.document_url] + body.supporting_urls, settings.confluence_base_url
    )
    unknown = sorted(set(body.models) - set(DOCUMENT_ROLES))
    if unknown:
        raise HTTPException(400, f"Unknown model roles: {', '.join(unknown)}")
    models = check_models(body.models, settings)
    from reviewer.accounts.credentials import Credentials, CredentialUnavailable
    from reviewer.services.docs.confluence import Confluence

    service = Credentials(request.app.state.store, settings)
    try:
        refs = await service.pin(request.state.principal.id)
        context = await service.load(request.state.principal.id, refs)
    except CredentialUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    if not context.tokens.get("confluence"):
        raise HTTPException(400, "A personal Confluence credential is required")
    factory = getattr(request.app.state, "personal_docs_factory", Confluence)
    docs = factory(settings.confluence_base_url, context.tokens["confluence"])
    try:
        principal_id = str(await docs.identity())
        ref = await docs.resolve(body.document_url)
        page = await docs.fetch(ref) if ref else None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403, 404}:
            raise HTTPException(404, "Page not found") from None
        raise HTTPException(502, "Confluence rejected the lookup") from None
    except httpx.HTTPError:
        raise HTTPException(502, "Confluence is unreachable") from None
    finally:
        await docs.close()
    if not principal_id:
        raise HTTPException(400, "Confluence did not identify the credential")
    if page is None:
        raise HTTPException(404, "Page is not visible to the reviewer")
    instruction = (body.instruction or "").strip() or None
    job = DocumentJob(
        event_id=hashlib.sha256(
            f"document:{page.page_id}:{uuid4()}".encode()
        ).hexdigest(),
        subject=DocumentSubject(
            page_id=str(page.page_id),
            space=page.space,
            title=page.title,
            url=body.document_url,
            version=page.version,
        ),
        overrides=ReviewOverrides(
            requested_by=request.state.principal.id,
            report_mode=body.report_mode,
            models=models,
            instruction=instruction,
            supporting_urls=list(dict.fromkeys(body.supporting_urls)),
            check_space=body.check_space,
        ),
        principal_id=principal_id,
    )
    from reviewer.store.models import ReviewTrigger

    async with request.app.state.store.transaction() as session:
        session.add(
            ReviewTrigger(
                event_id=job.event_id,
                owner_user_id=request.state.principal.id,
                credential_refs=refs,
                payload=job.model_dump(),
            )
        )
    try:
        async with asyncio.timeout(0.35):
            await request.app.state.queue.enqueue_job(
                "receive_event", job.model_dump(), _job_id=job.event_id
            )
        logger.info(
            "document_review_enqueued",
            page_id=page.page_id,
            version=page.version,
            event_id=job.event_id,
            report_mode=body.report_mode,
            check_space=body.check_space,
        )
    except Exception:
        logger.error("document_review_enqueue_failed", event_id=job.event_id)
        # The committed trigger remains accepted; recovery will enqueue it.
    return {
        "accepted": True,
        "kind": "document",
        "subject": job.subject.model_dump(),
        "event_id": job.event_id,
        "report_mode": body.report_mode,
        "models": models,
        "findings": None,
        "poll": f"/admin/reviews?event_id={job.event_id}",
    }


async def replay_document(review, request):
    """Re-run a document review at the page's current version with its overrides."""
    from reviewer.accounts.credentials import Credentials, CredentialUnavailable
    from reviewer.services.docs.confluence import Confluence, DocumentRef
    from reviewer.store.models import ReviewTrigger

    settings = request.app.state.settings
    service = Credentials(request.app.state.store, settings)
    try:
        refs = await service.pin(request.state.principal.id)
        context = await service.load(request.state.principal.id, refs)
    except CredentialUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    if not context.tokens.get("confluence"):
        raise HTTPException(400, "A personal Confluence credential is required")
    factory = getattr(request.app.state, "personal_docs_factory", Confluence)
    docs = factory(settings.confluence_base_url, context.tokens["confluence"])
    subject = dict(review.subject or {})
    try:
        principal_id = str(await docs.identity())
        page = await docs.fetch(
            DocumentRef(page_id=subject["page_id"], url=subject.get("url", ""))
        )
    except httpx.HTTPError:
        raise HTTPException(502, "Confluence lookup is unavailable") from None
    finally:
        await docs.close()
    if page is None:
        raise HTTPException(404, "Page is not visible to the reviewer")
    job = DocumentJob(
        event_id=str(uuid4()),
        subject=DocumentSubject(
            page_id=str(page.page_id),
            space=page.space,
            title=page.title,
            url=subject.get("url") or page.url,
            version=page.version,
        ),
        overrides=ReviewOverrides.model_validate(review.overrides or {}),
        principal_id=principal_id,
    )
    async with request.app.state.store.transaction() as session:
        session.add(
            ReviewTrigger(
                event_id=job.event_id,
                owner_user_id=request.state.principal.id,
                credential_refs=refs,
                payload=job.model_dump(),
            )
        )
    try:
        await request.app.state.queue.enqueue_job(
            "receive_event", job.model_dump(), _job_id=job.event_id
        )
    except Exception:
        pass  # The durable trigger is recovered independently of this request.
    return {"accepted": True, "event_id": job.event_id}
