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


async def test_two_owners_sharing_forge_identity_do_not_collect_each_others_threads(
    store, forge
):
    from reviewer.publish.publisher import existing
    from reviewer.services.forge.gitlab import Discussion, Note

    fingerprint = "a" * 32
    forge.discussions = [
        Discussion(
            id="personal",
            file="a.py",
            notes=[
                Note(
                    id=1,
                    author_id=forge.bot_id,
                    body=f"<!-- ai-review:fingerprint={fingerprint} -->\n<!-- ai-review:owner=alice -->",
                )
            ],
        )
    ]
    assert (await existing(forge, 7, 2))[0] == {}
    forge.owner_user_id = "bob"
    assert (await existing(forge, 7, 2))[0] == {}
    forge.owner_user_id = "alice"
    assert fingerprint in (await existing(forge, 7, 2))[0]


async def test_invalid_secret_inputs_never_echo_values(store, forge):
    from httpx import ASGITransport, AsyncClient

    from reviewer.config.schema import Settings
    from reviewer.main import create_app

    app = create_app(
        Settings(
            _env_file=None, session_origin="http://localhost", session_local_http=True
        ),
        store=store,
        forge=forge,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        secret = "private"
        response = await client.post(
            "/auth/activate",
            json={"token": "activation-secret", "password": secret},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 422
        assert secret not in response.text
        assert "activation-secret" not in response.text
        assert response.headers["cache-control"] == "no-store"


async def test_outbox_persists_ownership_and_conflict(store, forge):
    from httpx import ASGITransport, AsyncClient

    from reviewer.config.schema import Settings
    from reviewer.main import create_app
    from reviewer.store.models import ReviewTrigger
    from reviewer.worker import receive_event
    from tests.account_helpers import signed_in
    from tests.conftest import FakeQueue

    queue = FakeQueue()
    forge.projects["group/proj"] = 7
    settings = Settings(_env_file=None)
    app = create_app(settings, store, queue, forge)
    a_headers = await signed_in(app, "user", forge)
    a = app.state.test_account.id
    b_headers = await signed_in(app, "user", forge)
    b = app.state.test_account.id
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.post(
            "/admin/reviews",
            headers=a_headers,
            json={
                "merge_request_url": "https://gitlab.example.invalid/group/proj/-/merge_requests/2"
            },
        )
        assert response.status_code == 202
        event_id = response.json()["event_id"]
        assert (
            await client.get(f"/admin/reviews?event_id={event_id}", headers=b_headers)
        ).status_code == 404
        async with store.sessions() as session:
            trigger = await session.get(ReviewTrigger, event_id)
            assert trigger.owner_user_id == a
            assert "test-gitlab-token" not in str(trigger.payload)
            assert "test-gateway-token" not in str(trigger.payload)
        await store.accept(
            7,
            2,
            forge.mr.head_sha,
            "b-active",
            owner_user_id=b,
            trigger_source="manual",
        )
        await receive_event(
            {
                "store": store,
                "settings": settings,
                "forge": forge,
                "redis": queue,
                "personal_forge_factory": lambda *_: forge,
            },
            queue.jobs[0][1][0],
        )
        result = (
            await client.get(f"/admin/reviews?event_id={event_id}", headers=a_headers)
        ).json()
        assert result["state"] == "CONFLICT"
        assert b not in str(result)


async def test_personal_m0_worker_retains_limited_state_machine(store, forge):
    from reviewer.accounts.credentials import Credentials
    from reviewer.accounts.execution import personal_machine
    from reviewer.config.schema import Settings
    from reviewer.main import create_app
    from reviewer.orchestrator.machine import ReviewStateMachine
    from tests.account_helpers import signed_in

    settings = Settings(_env_file=None, milestone="M0")
    app = create_app(settings, store=store, forge=forge)
    await signed_in(app, "user", forge)
    owner = app.state.test_account.id
    refs = await Credentials(store, settings).pin(owner)
    review = await store.accept(
        7,
        2,
        forge.mr.head_sha,
        "personal-m0",
        owner_user_id=owner,
        credential_refs=refs,
        principal_id=str(forge.bot_id),
        trigger_source="manual",
    )
    ctx = {
        "settings": settings,
        "store": store,
        "machine": ReviewStateMachine(store, forge),
        "personal_forge_factory": lambda *_: forge,
    }
    async with personal_machine(ctx, review) as machine:
        assert isinstance(machine, ReviewStateMachine)
        await machine.run(review.id)
    assert (await store.get(review.id)).state == "PUBLISHED"
    assert await store.stages(review.id) == {}
    assert await store.findings_for(review.id) == []


async def test_personal_git_clients_share_mirror_leases(tmp_path):
    from reviewer.services.git.service import GitService

    first, second = GitService(tmp_path), GitService(tmp_path)
    entered = asyncio.Event()

    async def contender():
        async with second.mirror_lock(7):
            entered.set()

    async with first.mirror_lock(7):
        task = asyncio.create_task(contender())
        await asyncio.sleep(0.1)
        assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set()
