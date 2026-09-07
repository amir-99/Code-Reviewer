import asyncio
import hashlib
import hmac
from typing import Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from reviewer.context.models import ReviewOverrides

logger = structlog.get_logger()
router = APIRouter()


class ReviewJob(BaseModel):
    project_id: int = Field(gt=0)
    iid: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    event_id: str = Field(max_length=128)
    action: Literal["review", "cancel", "command", "pipeline"] = "review"
    user_id: int = 0
    command: str = Field(default="", max_length=1100)
    discussion_id: str | None = None
    note_id: int | None = None
    ci_status: Literal["success", "failed"] | None = None
    # Set only by the manual trigger; webhook-driven jobs leave it empty.
    overrides: ReviewOverrides | None = None


def event_job(payload: dict, event_id: str) -> ReviewJob | None:
    kind = payload.get("object_kind")
    if kind == "note":
        attrs = payload.get("object_attributes", {})
        mr = payload.get("merge_request") or {}
        if not mr or not attrs.get("note", "").strip().startswith("/ai "):
            return None
        return ReviewJob(
            project_id=payload["project"]["id"],
            iid=mr["iid"],
            head_sha=mr.get("last_commit", {}).get("id") or mr.get("sha") or "0" * 40,
            event_id=event_id,
            action="command",
            user_id=payload["user"]["id"],
            command=attrs["note"].strip(),
            discussion_id=attrs.get("discussion_id"),
            note_id=attrs.get("id"),
        )
    if kind == "pipeline":
        attrs = payload.get("object_attributes", {})
        if attrs.get("status") not in {"success", "failed"}:
            return None
        return ReviewJob(
            project_id=payload["project"]["id"],
            iid=payload.get("merge_request", {}).get("iid", 1),
            head_sha=attrs["sha"],
            event_id=event_id,
            action="pipeline",
            ci_status=attrs["status"],
        )
    if payload.get("object_kind") != "merge_request":
        return None
    attrs = payload.get("object_attributes", {})
    action = attrs.get("action")
    if action not in {"open", "reopen", "ready", "update", "merge", "close"}:
        return None
    if action not in {"merge", "close"} and (
        attrs.get("draft") or attrs.get("work_in_progress")
    ):
        return None
    sha = attrs.get("last_commit", {}).get("id", "")
    if action == "update" and (not attrs.get("oldrev") or attrs["oldrev"] == sha):
        return None
    return ReviewJob(
        project_id=payload["project"]["id"],
        iid=attrs["iid"],
        head_sha=sha,
        event_id=event_id,
        action="cancel" if action in {"merge", "close"} else "review",
    )


@router.post("/webhooks/gitlab")
async def gitlab_hook(request: Request):
    # Bound input before decoding; event text is never logged or stored in Redis.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_048_576:
            raise HTTPException(413, "Webhook exceeds size limit")
    import json

    try:
        payload = json.loads(body)
        project_id = int(payload["project"]["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid webhook") from None
    secret = request.app.state.settings.webhook_secrets.get(project_id)
    supplied = request.headers.get("x-gitlab-token", "")
    if (
        not secret
        or not secret.get_secret_value()
        or not hmac.compare_digest(
            supplied.encode(), secret.get_secret_value().encode()
        )
    ):
        raise HTTPException(401, "Invalid webhook token")
    # GitLab event UUID allows retries to deduplicate without retaining payloads.
    event_id = hashlib.sha256(
        f"{project_id}:{request.headers.get('x-gitlab-event-uuid') or uuid4()}".encode()
    ).hexdigest()
    try:
        job = event_job(payload, event_id)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid merge request event") from None
    if job is None:
        logger.info(
            "webhook_ignored",
            project_id=project_id,
            object_kind=payload.get("object_kind"),
            event_id=event_id,
        )
        return {"accepted": False, "reason": "event does not trigger a review"}
    try:
        async with asyncio.timeout(0.35):
            await request.app.state.queue.enqueue_job(
                "receive_event", job.model_dump(), _job_id=event_id
            )
        logger.info(
            "webhook_enqueued",
            project_id=job.project_id,
            iid=job.iid,
            action=job.action,
            event_id=event_id,
        )
    except Exception:
        logger.error(
            "webhook_enqueue_failed",
            project_id=job.project_id,
            iid=job.iid,
            event_id=event_id,
        )
        raise HTTPException(503, "Queue unavailable; retry webhook") from None
    return {"accepted": True}
