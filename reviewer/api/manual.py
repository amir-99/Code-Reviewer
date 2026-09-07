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
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from reviewer.api.admin import authenticate
from reviewer.api.webhooks import ReviewJob
from reviewer.context.models import ISSUE_KEY_PATTERN, ReviewOverrides

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
    # Admission would reject an unonboarded project inside the worker, where the
    # operator would never see it. Say so here instead of accepting a no-op.
    if not await request.app.state.store.is_configured(project_id):
        raise HTTPException(
            409, "Project is not onboarded; add it to WEBHOOK_SECRETS first"
        )
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
        ),
    )
    try:
        async with asyncio.timeout(0.35):
            await request.app.state.queue.enqueue_job(
                "receive_event", job.model_dump(), _job_id=job.event_id
            )
    except Exception:
        raise HTTPException(503, "Queue unavailable; retry the request") from None
    return {
        "accepted": True,
        "project_id": project_id,
        "iid": iid,
        "head_sha": mr.head_sha,
        "event_id": job.event_id,
    }
