"""Operator-triggered reviews.

The webhook path is driven by GitLab events; this one is driven by a person
pasting a merge request link, optionally naming the Jira story and epic and any
Confluence pages the automatic linkage would not have found. Admission still
happens in the worker through the same durable path a webhook uses, so a manual
run supersedes an in-flight review for the same merge request exactly as a new
push would.
"""

import asyncio
import hashlib
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from reviewer.api.admin import authenticate
from reviewer.api.webhooks import ReviewJob
from reviewer.config.loader import load_project
from reviewer.config.models import catalog
from reviewer.config.schema import ROLES
from reviewer.context.models import (
    ISSUE_KEY_PATTERN,
    ReportMode,
    ReviewOverrides,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/admin", dependencies=[Depends(authenticate)])

MERGE_REQUEST_PATH = re.compile(
    r"^(?P<path>.+?)/(?:-/)?merge_requests/(?P<iid>\d+)(?:/.*)?$"
)


def origin(url: str):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


def parse_merge_request_url(url: str, base_url: str) -> tuple[str, int]:
    """Split a merge request web URL into its project path and iid.

    Only URLs on the configured GitLab instance are accepted: the path is fed
    straight to the API with the reviewer's own token, so an arbitrary host here
    would be a request forgery with credentials attached.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Merge request URL must be http or https")
    if origin(url) != origin(base_url):
        raise ValueError("Merge request URL is not on the configured GitLab instance")
    prefix = urlsplit(base_url).path.rstrip("/")
    path = parts.path
    if prefix and not path.startswith(prefix + "/"):
        raise ValueError("Merge request URL is not on the configured GitLab instance")
    match = MERGE_REQUEST_PATH.match(path[len(prefix) :].strip("/"))
    if not match:
        raise ValueError("URL does not name a merge request")
    project_path = match["path"].strip("/")
    segments = project_path.split("/")
    # A project always lives under a namespace, and "-" is GitLab's own route
    # separator rather than a path segment.
    if len(segments) < 2 or any(s in {"", "-"} for s in segments):
        raise ValueError("URL does not name a merge request")
    if int(match["iid"]) < 1:
        raise ValueError("URL does not name a merge request")
    return project_path, int(match["iid"])


class ManualReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merge_request_url: str = Field(max_length=2048)
    issue_key: str | None = Field(default=None, pattern=ISSUE_KEY_PATTERN)
    epic_key: str | None = Field(default=None, pattern=ISSUE_KEY_PATTERN)
    document_urls: list[str] = Field(default_factory=list, max_length=20)
    # "applied" posts the report on the merge request, "draft" renders it for
    # GET /admin/reviews/{id} without writing to GitLab, and "none" publishes
    # nothing. A project configured for silent enforcement never posts, so
    # "applied" degrades to "none" there rather than overriding the operator.
    report_mode: ReportMode = "applied"
    # Model per role for this run only. Roles left out run on the project's
    # configured model, so an operator can move one stage without restating
    # the rest. Validated against the operator's catalogue below, because a
    # model this deployment is not configured for would only fail at the
    # gateway, one stage at a time.
    models: dict[str, str] = Field(default_factory=dict, max_length=len(ROLES))


def check_models(models, settings, project_id=None):
    """Reject unknown roles and models this installation cannot select."""
    if not models:
        return {}
    unknown = sorted(set(models) - set(ROLES))
    if unknown:
        raise HTTPException(400, f"Unknown model roles: {', '.join(unknown)}")
    config = load_project(settings.config_path, project_id or 0)
    allowed = set(catalog(settings, config))
    chosen = sorted({model for model in models.values() if model not in allowed})
    if chosen:
        raise HTTPException(
            400, f"Models are not configured for selection: {', '.join(chosen)}"
        )
    return dict(models)


def check_document_urls(urls, confluence_base_url):
    for url in urls:
        if len(url) > 2048 or urlsplit(url).scheme not in {"http", "https"}:
            raise HTTPException(400, "Document URLs must be http or https")
        # Confluence rejects foreign origins at fetch time; failing here tells
        # the operator why a page was ignored instead of silently dropping it.
        if confluence_base_url and origin(url) != origin(confluence_base_url):
            raise HTTPException(
                400, "Document URLs must be on the configured Confluence instance"
            )


@router.post("/reviews", status_code=202)
async def trigger(body: ManualReviewRequest, request: Request):
    settings = request.app.state.settings
    try:
        project_path, iid = parse_merge_request_url(
            body.merge_request_url, settings.gitlab_base_url
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    check_document_urls(body.document_urls, settings.confluence_base_url)
    forge = request.app.state.forge
    try:
        project_id = await forge.project_id_for_path(project_path)
        if project_id is None:
            raise HTTPException(404, "Project is not visible to the reviewer")
        mr = await forge.get_merge_request(project_id, iid)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {403, 404}:
            raise HTTPException(404, "Merge request not found") from None
        raise HTTPException(502, "GitLab rejected the lookup") from None
    except httpx.HTTPError:
        raise HTTPException(502, "GitLab is unreachable") from None
    if mr.state != "opened" or mr.draft:
        raise HTTPException(409, "Merge request is closed, merged or a draft")
    # Checked against the project's own catalogue, which is why it waits until
    # the URL has been resolved to a project.
    models = check_models(body.models, settings, project_id)
    job = ReviewJob(
        project_id=project_id,
        iid=iid,
        head_sha=mr.head_sha,
        # A fresh id every call: a manual re-trigger is a deliberate re-run, not
        # a webhook retry to be deduplicated away.
        event_id=hashlib.sha256(
            f"manual:{project_id}:{iid}:{uuid4()}".encode()
        ).hexdigest(),
        overrides=ReviewOverrides(
            issue_key=body.issue_key,
            epic_key=body.epic_key,
            document_urls=list(dict.fromkeys(body.document_urls)),
            requested_by="admin",
            report_mode=body.report_mode,
            models=models,
        ),
    )
    try:
        async with asyncio.timeout(0.35):
            await request.app.state.queue.enqueue_job(
                "receive_event", job.model_dump(), _job_id=job.event_id
            )
        logger.info(
            "manual_review_enqueued",
            project_id=project_id,
            iid=iid,
            head_sha=mr.head_sha,
            event_id=job.event_id,
            report_mode=body.report_mode,
            models=models or None,
        )
    except Exception:
        logger.error(
            "manual_review_enqueue_failed",
            project_id=project_id,
            iid=iid,
            event_id=job.event_id,
        )
        raise HTTPException(503, "Queue unavailable; retry the request") from None
    # The review runs in the worker, so no findings exist yet. Hand back the
    # address to poll rather than pretending this call carries results.
    return {
        "accepted": True,
        "project_id": project_id,
        "iid": iid,
        "head_sha": mr.head_sha,
        "event_id": job.event_id,
        "report_mode": body.report_mode,
        "models": models,
        "findings": None,
        "poll": f"/admin/reviews?event_id={job.event_id}",
    }
