"""Owners can replace tokens; no endpoint can reveal stored plaintext."""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, SecretStr

from reviewer.accounts.credentials import (
    INTEGRATIONS,
    Credentials,
    CredentialUnavailable,
)
from reviewer.api.accounts import Input, user
from reviewer.store.models import IntegrationCredential, utcnow

router = APIRouter(prefix="/profile", dependencies=[Depends(user)])


class Token(Input):
    token: SecretStr = Field(min_length=1, max_length=16384)


def credentials(request):
    return Credentials(request.app.state.store, request.app.state.settings)


def integration_name(integration):
    if integration not in INTEGRATIONS:
        raise HTTPException(404, "Integration not found")


@router.get("/integrations")
async def status(request: Request):
    return await credentials(request).status(request.state.principal.id)


@router.put("/integrations/{integration}")
async def save(integration: str, body: Token, request: Request):
    integration_name(integration)
    try:
        await credentials(request).save(
            request.state.principal.id, integration, body.token.get_secret_value()
        )
    except CredentialUnavailable as exc:
        raise HTTPException(400, str(exc)) from None
    return await status(request)


@router.delete("/integrations/{integration}")
async def remove(integration: str, request: Request):
    integration_name(integration)
    await credentials(request).remove(request.state.principal.id, integration)
    return await status(request)


@router.post("/integrations/{integration}/check")
async def check(integration: str, request: Request):
    integration_name(integration)
    service = credentials(request)
    rows = await service.latest(request.state.principal.id)
    if integration not in rows:
        raise HTTPException(400, "Credential is missing")
    row = rows[integration]
    # Invalid tokens may be rechecked after upstream access has been repaired.
    if row.state != "active":
        raise HTTPException(400, "Credential is missing")
    settings = request.app.state.settings
    endpoints = {
        "gitlab": (settings.gitlab_base_url, "/api/v4/user"),
        "jira": (settings.jira_base_url, "/rest/api/2/myself"),
        "confluence": (settings.confluence_base_url, "/rest/api/user/current"),
        "gateway": (settings.gateway_base_url, settings.gateway_auth_check_path),
    }
    base, path = endpoints[integration]
    if not base or not path:
        return {
            "checked": False,
            "reason": "No non-billable authentication endpoint is configured",
        }
    if not path.startswith("/") or path.startswith("//") or ":" in path or ".." in path:
        raise HTTPException(503, "Authentication endpoint is not configured")
    try:
        context = await service.load(
            row.user_id, {integration: row.id}, required=False, allow_invalid=True
        )
        token = context.tokens[integration]
        headers = (
            {"PRIVATE-TOKEN": token}
            if integration == "gitlab"
            else {"Authorization": f"Bearer {token}"}
        )
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(base.rstrip("/") + path, headers=headers)
        state = (
            "valid"
            if response.is_success
            else "invalid"
            if response.status_code in {401, 403}
            else "unavailable"
        )
    except (httpx.HTTPError, CredentialUnavailable):
        state = "unavailable"
    async with request.app.state.store.transaction() as session:
        saved = await session.get(IntegrationCredential, row.id)
        saved.validation_status = state
        saved.checked_at = utcnow()
    return {"checked": state == "valid", "status": state}
