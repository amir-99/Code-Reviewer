"""Chat API: the owner asks, admins read, and every ask is a durable queued job."""

import httpx
import pytest

from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.store.models import Review
from tests.account_helpers import signed_in
from tests.conftest import FakeQueue


@pytest.fixture
async def api(store, forge, tmp_path):
    config = tmp_path / "projects.json"
    config.write_text(
        '{"defaults":{"enforcement":"advisory","chat":{"messages_per_hour":2}}}'
    )
    app = create_app(
        Settings(_env_file=None, config_path=config), store, FakeQueue(), forge
    )
    headers = await signed_in(app, "user", forge)
    review = await store.accept(
        7,
        2,
        "a" * 40,
        "owner-chat",
        owner_user_id=app.state.test_account.id,
        principal_id=str(forge.bot_id),
        trigger_source="manual",
    )
    async with store.transaction() as session:
        row = await session.get(Review, review.id)
        row.state = "PUBLISHED"
    await store.save_snapshot(review.id, {"bundle": {}, "findings": []})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        yield app, client, review


async def test_owner_asks_and_the_question_is_queued_once(api, store):
    app, client, review = api
    url = f"/admin/reviews/{review.id}/chat"
    before = await client.get(url)
    assert before.status_code == 200
    assert before.json()["can_ask"] is True and before.json()["messages"] == []
    posted = await client.post(url, json={"question": "  Why was this blocked?  "})
    assert posted.status_code == 200, posted.text
    message = posted.json()["message"]
    assert message["sequence"] == 1 and message["status"] == "pending"
    assert message["question"] == "Why was this blocked?"
    assert "credential_refs" not in message
    assert app.state.queue.jobs == [("answer_chat", (message["id"],))]
    stored = await store.chat_message(message["id"])
    assert set(stored.credential_refs) >= {"gateway", "gitlab"}
    listed = await client.get(url)
    assert [m["id"] for m in listed.json()["messages"]] == [message["id"]]
    assert listed.json()["spend"]["tokens"] == 0
    detail = await client.get(f"/admin/reviews/{review.id}")
    assert detail.json()["capabilities"]["chat"] is True


async def test_questions_are_throttled_per_account(api):
    app, client, review = api
    url = f"/admin/reviews/{review.id}/chat"
    for _ in range(2):
        assert (await client.post(url, json={"question": "q"})).status_code == 200
    assert (await client.post(url, json={"question": "q"})).status_code == 429


async def test_only_the_owner_asks_and_admins_read(api, store, forge):
    app, client, review = api
    url = f"/admin/reviews/{review.id}/chat"
    await client.post(url, json={"question": "first"})
    admin = await signed_in(app, "admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=admin,
    ) as other:
        seen = await other.get(url)
        assert seen.status_code == 200
        assert [m["question"] for m in seen.json()["messages"]] == ["first"]
        assert seen.json()["can_ask"] is False
        assert (await other.post(url, json={"question": "q"})).status_code == 403
    stranger = await signed_in(app, "user", forge)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=stranger,
    ) as other:
        assert (await other.get(url)).status_code == 404
        assert (await other.post(url, json={"question": "q"})).status_code == 404


async def test_unfinished_or_disabled_reviews_refuse_questions(api, store):
    app, client, review = api
    url = f"/admin/reviews/{review.id}/chat"
    async with store.transaction() as session:
        row = await session.get(Review, review.id)
        row.state = "DEFECT_REVIEW"
    assert (await client.get(url)).json()["can_ask"] is False
    assert (await client.post(url, json={"question": "q"})).status_code == 409
    async with store.transaction() as session:
        row = await session.get(Review, review.id)
        row.state = "PUBLISHED"
    assert (await client.post(url, json={"question": "   "})).status_code == 422
    assert (
        await client.post(url, json={"question": "q", "extra": 1})
    ).status_code == 422
    app.state.settings.config_path.write_text('{"defaults":{"chat":{"enabled":false}}}')
    refused = await client.post(url, json={"question": "q"})
    assert refused.status_code == 409 and "disabled" in refused.json()["detail"]
