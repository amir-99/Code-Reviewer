"""Account transactions. Token plaintext exists only at issuance or verification."""

import asyncio
import hashlib
import secrets
import unicodedata
from datetime import UTC, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select, text, update

from reviewer.store.models import (
    Account,
    AccountEvent,
    AccountToken,
    IntegrationCredential,
    LoginSession,
    LoginThrottle,
    utcnow,
)

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
DUMMY_HASH = PASSWORDS.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def normalize(login):
    value = unicodedata.normalize("NFKC", login).strip().casefold()
    if not value or len(value) > 254 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid login")
    return value


def future(value):
    return (
        value.replace(tzinfo=UTC) > utcnow()
        if value.tzinfo is None
        else value > utcnow()
    )


def public(account):
    return {
        key: getattr(account, key)
        for key in ("id", "login", "display_name", "role", "active", "created_at")
    }


async def verify(password, hashed):
    try:
        return await asyncio.to_thread(PASSWORDS.verify, hashed or DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False


async def lock_accounts(session):
    # A single transaction lock prevents simultaneous last-admin demotions and bootstrap races.
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(72431001)"))


async def revoke(session, user_id):
    await session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))


async def issue(session, user_id):
    await session.execute(delete(AccountToken).where(AccountToken.user_id == user_id))
    token = secrets.token_urlsafe(32)
    session.add(
        AccountToken(
            token_hash=digest(token),
            user_id=user_id,
            expires_at=utcnow() + timedelta(hours=1),
        )
    )
    return token


class Accounts:
    def __init__(self, store):
        self.store = store

    async def create(self, login, display_name, role, actor_id=None, bootstrap=False):
        if (
            role not in {"admin", "user"}
            or not display_name.strip()
            or len(display_name) > 200
        ):
            raise ValueError("Invalid account")
        async with self.store.transaction() as session:
            await lock_accounts(session)
            if bootstrap and await session.scalar(select(Account.id).limit(1)):
                raise ValueError("Bootstrap requires an empty account table")
            login = normalize(login)
            if await session.scalar(select(Account.id).where(Account.login == login)):
                raise ValueError("Login is unavailable")
            account = Account(login=login, display_name=display_name.strip(), role=role)
            session.add(account)
            await session.flush()
            token = await issue(session, account.id)
            session.add(
                AccountEvent(actor_id=actor_id, user_id=account.id, action="created")
            )
            return public(account), token

    async def activate(self, token, password):
        if not 12 <= len(password) <= 1024:
            raise ValueError("Password must contain 12 to 1024 characters")
        hashed = await asyncio.to_thread(PASSWORDS.hash, password)
        async with self.store.transaction() as session:
            row = await session.scalar(
                select(AccountToken)
                .where(AccountToken.token_hash == digest(token))
                .with_for_update()
            )
            if row is None or not future(row.expires_at):
                raise ValueError("Invalid or expired activation")
            account = await session.scalar(
                select(Account).where(Account.id == row.user_id).with_for_update()
            )
            if not account.active:
                raise ValueError("Invalid or expired activation")
            account.password_hash = hashed
            account.updated_at = utcnow()
            await revoke(session, account.id)
            await session.delete(row)
            session.add(
                AccountEvent(
                    user_id=account.id, actor_id=account.id, action="password_set"
                )
            )

    async def throttle(self, key):
        async with self.store.transaction() as session:
            await lock_accounts(session)
            key = digest(key)
            row = await session.get(LoginThrottle, key)
            if row is None:
                row = LoginThrottle(
                    key=key, attempts=0, expires_at=utcnow() + timedelta(minutes=15)
                )
                session.add(row)
            elif not future(row.expires_at):
                row.attempts = 0
                row.expires_at = utcnow() + timedelta(minutes=15)
            row.attempts += 1
            return row.attempts <= 10

    async def login(self, login, password, client, old_token=None):
        try:
            normalized = normalize(login)
        except ValueError:
            normalized = ""
        # Both address and account limits are durable across API workers/restarts.
        allowed = await self.throttle("ip:" + client)
        allowed = await self.throttle("login:" + normalized) and allowed
        if not allowed:
            raise ValueError("Invalid credentials or temporarily unavailable")
        async with self.store.sessions() as session:
            account = await session.scalar(
                select(Account).where(Account.login == normalized)
            )
        valid = await verify(password, account.password_hash if account else None)
        if (
            not valid
            or account is None
            or not account.active
            or account.password_hash is None
        ):
            raise ValueError("Invalid credentials or temporarily unavailable")
        async with self.store.transaction() as session:
            current = await session.scalar(
                select(Account).where(Account.id == account.id).with_for_update()
            )
            if (
                not current.active
                or current.password_hash != account.password_hash
                or current.role != account.role
            ):
                raise ValueError("Invalid credentials or temporarily unavailable")
            if old_token:
                await session.execute(
                    delete(LoginSession).where(
                        LoginSession.token_hash == digest(old_token)
                    )
                )
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session.add(
                LoginSession(
                    token_hash=digest(token),
                    csrf_hash=digest(csrf),
                    user_id=account.id,
                    expires_at=utcnow() + timedelta(hours=12),
                )
            )
            return public(current), token, csrf

    async def resolve(self, token):
        if not token:
            return None
        async with self.store.sessions() as session:
            row = await session.get(LoginSession, digest(token))
            if row is None or not future(row.expires_at):
                return None
            account = await session.get(Account, row.user_id)
            return (account, row) if account and account.active else None

    async def manage(self, actor_id, user_id, action, value=None):
        async with self.store.transaction() as session:
            await lock_accounts(session)
            account = await session.scalar(
                select(Account).where(Account.id == user_id).with_for_update()
            )
            if account is None:
                raise ValueError("Account not found")
            if action in {"role", "active"}:
                if action == "role" and value not in {"admin", "user"}:
                    raise ValueError("Invalid role")
                if action == "active" and not isinstance(value, bool):
                    raise ValueError("Invalid status")
                removing = (action == "role" and value != "admin") or (
                    action == "active" and not value
                )
                if account.role == "admin" and account.active and removing:
                    others = await session.scalar(
                        select(Account.id)
                        .where(
                            Account.role == "admin",
                            Account.active.is_(True),
                            Account.id != user_id,
                        )
                        .limit(1)
                    )
                    if not others:
                        raise ValueError("Cannot remove the last active admin")
                setattr(account, action, value)
            elif action == "recovery":
                # Operator-delivered recovery cannot convey access to saved integrations.
                await session.execute(
                    update(IntegrationCredential)
                    .where(IntegrationCredential.user_id == user_id)
                    .values(state="revoked")
                )
                account.password_hash = None
            elif action != "revoke_sessions":
                raise ValueError("Invalid action")
            await revoke(session, user_id)
            await session.execute(
                delete(AccountToken).where(AccountToken.user_id == user_id)
            )
            account.updated_at = utcnow()
            session.add(AccountEvent(actor_id=actor_id, user_id=user_id, action=action))
            return await issue(session, user_id) if action == "recovery" else None

    async def logout(self, token):
        async with self.store.transaction() as session:
            await session.execute(
                delete(LoginSession).where(LoginSession.token_hash == digest(token))
            )
