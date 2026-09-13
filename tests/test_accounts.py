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


async def test_personal_admission_conflicts_and_reuse_are_scoped(store):
    accounts = Accounts(store)
    a = await active(accounts, "a")
    b = await active(accounts, "b")
    first = await store.accept(
        7, 2, "a" * 40, "first", owner_user_id=a["id"], trigger_source="manual"
    )
    assert (
        await store.accept(
            7, 2, "a" * 40, "other", owner_user_id=b["id"], trigger_source="manual"
        )
        is None
    )
    assert (await store.get(first.id)).state == "INIT"
    replacement = await store.accept(
        7, 2, "a" * 40, "again", owner_user_id=a["id"], trigger_source="manual"
    )
    assert (await store.get(first.id)).state == "SUPERSEDED"
    assert replacement.owner_user_id == a["id"]
    assert await store.accept(7, 2, "a" * 40, "command") is None
    system = await store.accept(7, 2, "b" * 40, "head-change")
    assert system.owner_user_id is None
    assert (await store.get(replacement.id)).state == "SUPERSEDED"


async def test_cookie_api_denies_shared_token_and_admin_execution(store, forge):
    from httpx import ASGITransport, AsyncClient

    from reviewer.config.schema import Settings
    from reviewer.main import create_app
    from tests.conftest import FakeQueue

    accounts = Accounts(store)
    admin = await active(accounts, "admin", "admin")
    owner = await active(accounts, "owner")
    await active(accounts, "other")
    review = await store.accept(
        7, 2, "a" * 40, "owned", owner_user_id=owner["id"], trigger_source="manual"
    )
    app = create_app(
        Settings(
            _env_file=None,
            session_origin="http://localhost",
            session_local_http=True,
            admin_token="legacy",
        ),
        store,
        FakeQueue(),
        forge,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (
            await client.get(
                "/admin/reviews", headers={"Authorization": "Bearer legacy"}
            )
        ).status_code == 401
        login = await client.post(
            "/auth/login",
            json={"login": "admin", "password": "a sufficiently long password"},
            headers={"Origin": "http://localhost"},
        )
        assert login.status_code == 200
        headers = {
            "Origin": "http://localhost",
            "X-CSRF-Token": login.json()["csrf_token"],
        }
        assert (await client.get(f"/admin/reviews/{review.id}")).status_code == 200
        assert (
            await client.post(f"/admin/reviews/{review.id}/replay", headers=headers)
        ).status_code == 403
        assert (
            await client.post(
                "/admin/reviews",
                json={
                    "merge_request_url": "https://gitlab.example.invalid/a/b/-/merge_requests/2"
                },
                headers=headers,
            )
        ).status_code == 403
        assert (
            await client.post("/auth/logout", headers={"Origin": "http://localhost"})
        ).status_code == 403
        await client.post("/auth/logout", headers=headers)
        login = await client.post(
            "/auth/login",
            json={"login": "other", "password": "a sufficiently long password"},
            headers={"Origin": "http://localhost"},
        )
        assert login.status_code == 200
        assert (await client.get(f"/admin/reviews/{review.id}")).status_code == 404
        assert (
            await client.get(f"/admin/reviews/{review.id}/audit")
        ).status_code == 404
        assert (
            await client.get(f"/admin/reviews/{review.id}/events")
        ).status_code == 404
        assert (await client.get("/admin/reviews?event_id=owned")).status_code == 404
        assert (await client.get("/admin/reviews")).json() == {"reviews": []}
        assert admin["role"] == "admin"


async def test_credentials_rotation_removal_and_owner_binding(store):
    import base64

    from reviewer.accounts.credentials import Credentials, CredentialUnavailable
    from reviewer.config.schema import Settings

    accounts = Accounts(store)
    a = await active(accounts, "a")
    b = await active(accounts, "b")
    settings = Settings(
        _env_file=None,
        credential_active_key="one",
        credential_keys={"one": base64.b64encode(b"k" * 32).decode()},
    )
    credentials = Credentials(store, settings)
    await credentials.save(a["id"], "gitlab", "owner-a-gitlab-token")
    with pytest.raises(CredentialUnavailable):
        await credentials.pin(a["id"])
    await credentials.save(a["id"], "gateway", "owner-a-gateway-token")
    pinned = await credentials.pin(a["id"])
    await credentials.save(a["id"], "gitlab", "replacement-gitlab-token")
    assert (await credentials.load(a["id"], pinned)).tokens[
        "gitlab"
    ] == "owner-a-gitlab-token"
    assert (await credentials.load(a["id"], await credentials.pin(a["id"]))).tokens[
        "gitlab"
    ] == "replacement-gitlab-token"
    with pytest.raises(CredentialUnavailable):
        await credentials.load(b["id"], pinned)
    assert "owner-a" not in str(await credentials.status(a["id"]))
    async with store.sessions() as session:
        rows = (await session.scalars(select(IntegrationCredential))).all()
        assert all("token" not in row.ciphertext for row in rows)
    await credentials.remove(a["id"], "gitlab")
    with pytest.raises(CredentialUnavailable):
        await credentials.load(a["id"], pinned)


async def test_personal_forge_rechecks_eligibility_and_author_before_writes(forge):
    from reviewer.accounts.credentials import CredentialUnavailable
    from reviewer.accounts.execution import PersonalForge

    allowed = True

    async def guard():
        if not allowed:
            raise CredentialUnavailable("revoked")

    client = PersonalForge(forge, guard, str(forge.bot_id), 7, 2, forge.mr.head_sha)
    await client.post_note(7, 2, "report")
    count = len(forge.comments)
    allowed = False
    with pytest.raises(CredentialUnavailable):
        await client.post_note(7, 2, "must not publish")
    assert len(forge.comments) == count
    allowed = True
    forge.bot_id += 1
    with pytest.raises(CredentialUnavailable):
        await client.post_note(7, 2, "wrong identity")
    assert len(forge.comments) == count
