"""Rerun and retry: who may run a review again, and what each one repeats."""

import json

import httpx
import pytest

from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.store.models import ReviewTrigger
from tests.account_helpers import signed_in
from tests.conftest import FakeQueue


@pytest.fixture
async def api(store, forge):
    app = create_app(Settings(_env_file=None), store, FakeQueue(), forge)
    headers = await signed_in(app, "user", forge)
    review = await store.accept(
        7,
        2,
        "a" * 40,
        "failed-run",
        {"issue_key": "ABC-1", "report_mode": "draft"},
        owner_user_id=app.state.test_account.id,
        principal_id=str(forge.bot_id),
        trigger_source="manual",
    )
    await store.transition(
        review.id, "FAILED_INTERNAL", partial=True, error="gateway unavailable"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        yield app, client, review


def snapshot(stream):
    """The first snapshot frame of a finished review's event stream."""
    for block in stream.split("\n\n"):
        if "event: snapshot" in block:
            line = next(x for x in block.splitlines() if x.startswith("data: "))
            return json.loads(line[len("data: ") :])
    raise AssertionError("no snapshot frame")


async def test_owner_receives_controls_everywhere_the_dashboard_reads(api):
    app, client, review = api
    allowed = {
        "rerun": {"allowed": True, "reason": None},
        "retry": {"allowed": True, "reason": None},
    }
    detail = await client.get(f"/admin/reviews/{review.id}")
    assert detail.json()["controls"] == allowed
    listed = (await client.get("/admin/reviews")).json()["reviews"]
    assert listed[0]["controls"] == allowed
    events = await client.get(f"/admin/reviews/{review.id}/events")
    assert snapshot(events.text)["controls"] == allowed


async def test_retry_repeats_the_failed_run_on_its_head(api, store):
    app, client, review = api
    response = await client.post(f"/admin/reviews/{review.id}/retry")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "retry"
    assert body["poll"] == f"/admin/reviews?event_id={body['event_id']}"
    [(name, (payload,))] = app.state.queue.jobs
    assert name == "receive_event"
    assert payload["head_sha"] == review.head_sha
    assert payload["overrides"]["issue_key"] == "ABC-1"
    assert payload["overrides"]["report_mode"] == "draft"
    async with store.sessions() as session:
        trigger = await session.get(ReviewTrigger, body["event_id"])
    assert trigger.state == "QUEUED"
    assert trigger.owner_user_id == app.state.test_account.id
    requested = [e for e in await store.events(review.id) if e["kind"] == "request"]
    assert requested[-1]["data"]["event_id"] == body["event_id"]
    assert requested[-1]["data"]["name"].startswith("Retry requested by")


async def test_retry_is_refused_once_the_merge_request_moved_on(api, forge):
    app, client, review = api
    forge.mr.head_sha = "b" * 40
    response = await client.post(f"/admin/reviews/{review.id}/retry")
    assert response.status_code == 409
    assert "new commits" in response.json()["detail"]
    assert app.state.queue.jobs == []
    # The fresh head is what a rerun is for.
    rerun = await client.post(f"/admin/reviews/{review.id}/rerun")
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["mode"] == "rerun"
    [(_, (payload,))] = app.state.queue.jobs
    assert payload["head_sha"] == "b" * 40


async def test_retry_is_refused_for_a_review_that_did_not_fail(api, store):
    app, client, review = api
    finished = await store.accept(
        7,
        2,
        "a" * 40,
        "finished-run",
        owner_user_id=app.state.test_account.id,
        trigger_source="manual",
    )
    await store.transition(finished.id, "CONTEXT_COLLECTION")
    running = (await client.get(f"/admin/reviews/{finished.id}")).json()["controls"]
    assert running["retry"] == {
        "allowed": False,
        "reason": "The review is still running",
    }
    assert running["rerun"]["allowed"] is True
    await store.transition(finished.id, "CANCELLED")
    controls = (await client.get(f"/admin/reviews/{finished.id}")).json()["controls"]
    assert controls["retry"]["reason"] == "Only a failed review can be retried"
    response = await client.post(f"/admin/reviews/{finished.id}/retry")
    assert response.status_code == 409
    assert response.json()["detail"] == "Only a failed review can be retried"
    assert app.state.queue.jobs == []


async def test_a_later_review_overtakes_the_failed_one(api, store):
    app, client, review = api
    await store.accept(7, 2, "a" * 40, "webhook-run")
    controls = (await client.get(f"/admin/reviews/{review.id}")).json()["controls"]
    assert controls["retry"]["allowed"] is False
    assert "later review" in controls["retry"]["reason"]
    assert controls["rerun"]["allowed"] is True
    listed = {
        r["id"]: r for r in (await client.get("/admin/reviews")).json()["reviews"]
    }
    assert listed[review.id]["controls"]["retry"]["allowed"] is False
    response = await client.post(f"/admin/reviews/{review.id}/retry")
    assert response.status_code == 409
    assert app.state.queue.jobs == []


async def test_admins_see_controls_refused_and_cannot_execute(api, store):
    app, client, review = api
    headers = await signed_in(app, "admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as admin:
        controls = (await admin.get(f"/admin/reviews/{review.id}")).json()["controls"]
        assert controls["retry"]["allowed"] is False
        assert controls["rerun"]["allowed"] is False
        assert controls["rerun"]["reason"] == "Only the review's owner can run it again"
        for action in ("retry", "rerun", "replay"):
            response = await admin.post(f"/admin/reviews/{review.id}/{action}")
            assert response.status_code == 403
    assert app.state.queue.jobs == []
