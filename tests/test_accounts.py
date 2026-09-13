import asyncio

import pytest
from sqlalchemy import select

from reviewer.accounts.service import Accounts, digest
from reviewer.store.models import (
    Account,
    AccountToken,
    IntegrationCredential,
    LoginSession,
)


async def active(accounts, login="alice", role="user", bootstrap=False):
    user, token = await accounts.create(login, login, role, bootstrap=bootstrap)
    await accounts.activate(token, "a sufficiently long password")
    return user


async def test_activation_single_use_and_hashed_sessions(store):
    accounts = Accounts(store)
    user, token = await accounts.create(" Alice ", "Alice", "user")
    async with store.sessions() as session:
        assert await session.get(AccountToken, token) is None
        assert await session.get(AccountToken, digest(token)) is not None
    await accounts.activate(token, "a sufficiently long password")
    with pytest.raises(ValueError):
        await accounts.activate(token, "another sufficiently long password")
    who, session_token, csrf = await accounts.login(
        "ALICE", "a sufficiently long password", "local"
    )
    assert who["id"] == user["id"]
    async with store.sessions() as session:
        saved = await session.get(LoginSession, digest(session_token))
        assert saved.csrf_hash == digest(csrf)
        assert await session.get(LoginSession, session_token) is None
        assert (await session.get(Account, user["id"])).password_hash.startswith(
            "$argon2id$"
        )
    assert await accounts.resolve(session_token)
    await accounts.logout(session_token)
    assert await accounts.resolve(session_token) is None


async def test_last_admin_concurrent_demotion_and_bootstrap(store):
    accounts = Accounts(store)
    first = await active(accounts, "first", "admin", True)
    with pytest.raises(ValueError):
        await accounts.create("bootstrap2", "Second", "admin", bootstrap=True)
    second = await active(accounts, "second", "admin")
    results = await asyncio.gather(
        *[
            accounts.manage(first["id"], user["id"], "role", "user")
            for user in (first, second)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    async with store.sessions() as session:
        admins = (
            await session.scalars(
                select(Account).where(Account.role == "admin", Account.active.is_(True))
            )
        ).all()
        assert len(admins) == 1


async def test_recovery_revokes_integrations_and_sessions(store):
    accounts = Accounts(store)
    user = await active(accounts)
    _, session_token, _ = await accounts.login(
        "alice", "a sufficiently long password", "local"
    )
    async with store.transaction() as session:
        session.add(
            IntegrationCredential(
                user_id=user["id"],
                integration="gitlab",
                version=1,
                key_id="key",
                ciphertext="encrypted",
            )
        )
    token = await accounts.manage(user["id"], user["id"], "recovery")
    assert await accounts.resolve(session_token) is None
    async with store.sessions() as session:
        assert (await session.scalar(select(IntegrationCredential))).state == "revoked"
    await accounts.activate(token, "a brand new long password")
    with pytest.raises(ValueError):
        await accounts.login("alice", "a sufficiently long password", "local")


async def test_role_and_disablement_revoke_sessions(store):
    accounts = Accounts(store)
    user = await active(accounts)
    _, token, _ = await accounts.login("alice", "a sufficiently long password", "local")
    await accounts.manage(user["id"], user["id"], "role", "admin")
    assert await accounts.resolve(token) is None
    with pytest.raises(ValueError):
        await accounts.manage(user["id"], user["id"], "active", False)


async def test_login_throttle_is_durable(store):
    accounts = Accounts(store)
    for _ in range(10):
        assert await accounts.throttle("address")
    assert not await Accounts(store).throttle("address")
