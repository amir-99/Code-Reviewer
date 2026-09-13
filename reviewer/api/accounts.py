"""Cookie authentication and local account administration."""

import hmac
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from reviewer.accounts.service import Accounts, digest, public
from reviewer.store.models import Account

router = APIRouter(prefix="/auth")
COOKIE = "reviewer_session"


def transport(request):
    settings = request.app.state.settings
    origin = settings.session_origin
    parsed = urlsplit(origin)
    if (
        not origin
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise HTTPException(503, "Account login is not configured")
    secure = parsed.scheme == "https"
    if not secure and not (
        settings.session_local_http
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise HTTPException(503, "Secure account login is not configured")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(
        "origin"
    ) != origin.rstrip("/"):
        raise HTTPException(403, "Invalid request origin")
    return secure


async def authenticate(request: Request):
    result = await Accounts(request.app.state.store).resolve(
        request.cookies.get(COOKIE)
    )
    if result is None:
        raise HTTPException(401, "Authentication required")
    transport(request)
    account, session = result
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if not hmac.compare_digest(
            session.csrf_hash, digest(request.headers.get("x-csrf-token", ""))
        ):
            raise HTTPException(403, "Invalid CSRF token")
    request.state.principal = account
    return account


async def admin(request: Request, account=Depends(authenticate)):
    if account.role != "admin":
        raise HTTPException(403, "Admin role required")
    return account


async def user(request: Request, account=Depends(authenticate)):
    if account.role != "user":
        raise HTTPException(403, "User role required")
    return account


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    login: str = Field(max_length=254)
    password: SecretStr = Field(max_length=1024)


class Activation(Input):
    token: SecretStr = Field(max_length=128)
    password: SecretStr = Field(min_length=12, max_length=1024)


class Create(Input):
    login: str = Field(min_length=1, max_length=254)
    display_name: str = Field(min_length=1, max_length=200)
    role: Literal["admin", "user"] = "user"


class Manage(Input):
    action: Literal["role", "active", "recovery", "revoke_sessions"]
    value: Literal["admin", "user"] | bool | None = None


def failure(exc):
    raise HTTPException(400, str(exc)) from None


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    secure = transport(request)
    try:
        account, token, csrf = await Accounts(request.app.state.store).login(
            body.login,
            body.password.get_secret_value(),
            request.client.host if request.client else "unknown",
            request.cookies.get(COOKIE),
        )
    except ValueError:
        raise HTTPException(
            401, "Invalid credentials or temporarily unavailable"
        ) from None
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=43200,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"account": account, "csrf_token": csrf}


@router.post("/activate")
async def activate(body: Activation, request: Request):
    transport(request)
    try:
        await Accounts(request.app.state.store).activate(
            body.token.get_secret_value(), body.password.get_secret_value()
        )
    except ValueError as exc:
        failure(exc)
    return {"activated": True}


@router.get("/me")
async def me(account=Depends(authenticate)):
    return {
        "account": public(account),
        "capabilities": {
            "execute": account.role == "user",
            "manage_accounts": account.role == "admin",
        },
    }


@router.post("/logout")
async def logout(request: Request, response: Response, account=Depends(authenticate)):
    await Accounts(request.app.state.store).logout(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"logged_out": True}


@router.get("/users")
async def users(
    request: Request, search: str = "", offset: int = 0, account=Depends(admin)
):
    async with request.app.state.store.sessions() as session:
        rows = await session.scalars(
            select(Account)
            .where(Account.login.contains(search[:254], autoescape=True))
            .order_by(Account.login)
            .offset(max(0, offset))
            .limit(100)
        )
        return {"users": [public(row) for row in rows]}


@router.post("/users", status_code=201)
async def create(body: Create, request: Request, account=Depends(admin)):
    try:
        created, token = await Accounts(request.app.state.store).create(
            body.login, body.display_name, body.role, account.id
        )
    except ValueError as exc:
        failure(exc)
    return {"account": created, "activation_token": token}


@router.post("/users/{user_id}")
async def manage(user_id: str, body: Manage, request: Request, account=Depends(admin)):
    try:
        token = await Accounts(request.app.state.store).manage(
            account.id, user_id, body.action, body.value
        )
    except ValueError as exc:
        failure(exc)
    return {"updated": True, "activation_token": token}


class PasswordChange(Input):
    current_password: SecretStr = Field(max_length=1024)
    password: SecretStr = Field(min_length=12, max_length=1024)


@router.post("/password")
async def change_password(
    body: PasswordChange, request: Request, account=Depends(authenticate)
):
    import asyncio

    from reviewer.accounts.service import PASSWORDS, revoke, verify
    from reviewer.store.models import AccountEvent, utcnow

    if not await verify(
        body.current_password.get_secret_value(), account.password_hash
    ):
        raise HTTPException(400, "Current password is incorrect")
    hashed = await asyncio.to_thread(PASSWORDS.hash, body.password.get_secret_value())
    async with request.app.state.store.transaction() as session:
        current = await session.scalar(
            select(Account).where(Account.id == account.id).with_for_update()
        )
        if not current.active or current.password_hash != account.password_hash:
            raise HTTPException(401, "Authentication required")
        current.password_hash = hashed
        current.updated_at = utcnow()
        await revoke(session, current.id)
        session.add(
            AccountEvent(
                actor_id=current.id, user_id=current.id, action="password_changed"
            )
        )
    return {"changed": True}
