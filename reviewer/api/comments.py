"""Owner-only comment actions, serialized with worker publication and rechecks."""

from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field

from reviewer.accounts.credentials import Credentials, CredentialUnavailable
from reviewer.accounts.execution import PersonalForge, personal_secrets
from reviewer.accounts.policy import review_access
from reviewer.api.accounts import Input, authenticate
from reviewer.config.loader import load_project
from reviewer.config.schema import ProjectConfig
from reviewer.publish.comments import CommentConflict, Comments
from reviewer.services.forge.gitlab import GitLab, PositionError, StaleReview

router = APIRouter(prefix="/admin/reviews", dependencies=[Depends(authenticate)])


class Action(Input):
    action: Literal[
        "edit", "remove", "resolve", "publish", "resolve_all", "publish_all"
    ]
    key: str | None = Field(default=None, max_length=40)
    revision: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, min_length=1, max_length=30000)


@asynccontextmanager
async def manager(request, review, mutate=False):
    store, settings = request.app.state.store, request.app.state.settings
    project = await store.project_number(review)
    config = load_project(settings.config_path, project)
    frozen = (review.execution_config or {}).get("config")
    if frozen:
        frozen = ProjectConfig.model_validate(frozen)
    if mutate:
        if config.enforcement == "silent" or (
            frozen and frozen.enforcement == "silent"
        ):
            raise HTTPException(
                409, "Comment writes are disabled by silent enforcement"
            )
        if review.state != "PUBLISHED" or review.superseded_by:
            raise HTTPException(
                409, "Only completed, current reviews can manage comments"
            )
    config = frozen or config
    service = Credentials(store, settings)
    # Use the pinned personal identity; never the installation's GitLab token.
    context = await service.load(review.owner_user_id, review.credential_refs or {})
    forge = (getattr(request.app.state, "personal_forge_factory", None) or GitLab)(
        settings.gitlab_base_url, context.tokens["gitlab"]
    )
    secret_token = personal_secrets.set(tuple(context.tokens.values()))
    try:

        async def guard():
            await service.check_access(context)

        if str(await forge.identity()) != review.principal_id:
            raise CredentialUnavailable("Publishing identity changed")
        client = PersonalForge(
            forge, guard, review.principal_id, project, review.mr_iid, review.head_sha
        )
        client.owner_user_id = review.owner_user_id
        async with store.mr_lock(project, review.mr_iid):
            latest = await store.get(review.id)
            if mutate and (latest.state != "PUBLISHED" or latest.superseded_by):
                raise HTTPException(409, "Review is no longer current")
            comments = Comments(store, client, review, project, config)
            mr = await client.get_merge_request(project, review.mr_iid)
            from reviewer.api.review_links import review_links

            comments.links = review_links(
                await store.snapshot(review.id) or {},
                review.overrides,
                settings,
                mr=mr.model_dump(),
            )
            comments.can_manage = (
                latest.state == "PUBLISHED"
                and not latest.superseded_by
                and config.enforcement != "silent"
                and load_project(settings.config_path, project).enforcement != "silent"
                and mr.head_sha == review.head_sha
                and mr.state == "opened"
                and not mr.draft
            )
            if mutate and not comments.can_manage:
                raise HTTPException(
                    409, "Review is no longer eligible for comment changes"
                )
            yield comments
    finally:
        try:
            await forge.close()
        finally:
            personal_secrets.reset(secret_token)


@router.get("/{review_id}/comments")
async def inspect(review_id: str, request: Request):
    review = await review_access(request, review_id)
    if request.state.principal.role == "admin":
        # Admins may inspect cached results, never personal integrations.
        rows = await request.app.state.store.comments_for(review_id)
        return {"comments": list(rows.values()), "can_manage": False, "synced": False}
    try:
        async with manager(request, review) as comments:
            rows = await comments.sync()
        return {
            "comments": rows,
            "links": comments.links,
            "can_manage": comments.can_manage,
            "synced": True,
        }
    except (httpx.HTTPError, CredentialUnavailable):
        rows = await request.app.state.store.comments_for(review_id)
        return {
            "comments": list(rows.values()),
            "can_manage": False,
            "synced": False,
            "error": "Could not refresh GitLab comments; showing last known state",
        }


@router.post("/{review_id}/comments")
async def act(review_id: str, body: Action, request: Request):
    review = await review_access(request, review_id, execute=True)
    bulk = body.action in {"resolve_all", "publish_all"}
    if not bulk and not body.key:
        raise HTTPException(422, "A comment key is required")
    if body.action == "edit" and (
        not body.message or not body.message.strip() or not body.revision
    ):
        raise HTTPException(422, "Message and revision are required for editing")
    results = []
    try:
        async with manager(request, review, mutate=True) as comments:
            await comments.sync()
            keys = list(comments.rows) if bulk else [body.key]
            action = {"resolve_all": "resolve", "publish_all": "publish"}.get(
                body.action, body.action
            )
            for key in keys:
                row = comments.rows.get(key) or {}
                if bulk and (
                    (
                        action == "resolve"
                        and (
                            row.get("status") != "committed"
                            or row.get("thread_status") != "open"
                        )
                    )
                    or (
                        action == "publish"
                        and (
                            row.get("status") in {"removed", "committed"}
                            or row.get("intent") == "remove"
                            or row.get("eligible") is False
                            or not row.get("body")
                            or (
                                key != "summary"
                                and row.get("status") != "drafted"
                                and not row.get("position")
                            )
                        )
                    )
                ):
                    results.append({"key": key, "status": "skipped"})
                    continue
                try:
                    result = await comments.act(
                        key, action, body.revision, body.message
                    )
                    results.append({"key": key, "status": result})
                except (
                    CommentConflict,
                    httpx.HTTPError,
                    CredentialUnavailable,
                    StaleReview,
                    PositionError,
                ) as exc:
                    error = (
                        str(exc)
                        if isinstance(exc, CommentConflict)
                        else "GitLab action unavailable or review eligibility changed"
                    )
                    results.append({"key": key, "status": "failed", "error": error})
            try:
                await comments.sync()
                synced = True
            except (httpx.HTTPError, CredentialUnavailable):
                synced = False
            return {
                "comments": comments.view(),
                "links": comments.links,
                "results": results,
                "synced": synced,
                "can_manage": synced and comments.can_manage,
            }
    except (CredentialUnavailable, StaleReview):
        raise HTTPException(
            409, "Personal credentials or review eligibility changed"
        ) from None
    except httpx.HTTPError:
        raise HTTPException(502, "GitLab comments are unavailable") from None
