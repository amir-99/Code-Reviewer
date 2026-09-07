import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeQueue
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.orchestrator.machine import ReviewStateMachine
from reviewer.store.models import Review, ReviewStage
from reviewer.worker import receive_event, recover


async def test_webhook_to_persisted_review_and_passing_status(store, forge):
    queue = FakeQueue()
    app = create_app(Settings(webhook_secrets={7: "test-token"}), store, queue)
    payload = json.loads(Path("tests/fixtures/mr_open.json").read_text())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/webhooks/gitlab",
                json=payload,
                headers={
                    "X-Gitlab-Token": "test-token",
                    "X-Gitlab-Event-UUID": "event-1",
                },
            )
            assert response.status_code == 200
    assert len(queue.jobs) == 1
    machine = ReviewStateMachine(store, forge)
    ctx = {"store": store, "forge": forge, "redis": queue, "machine": machine}
    await receive_event(ctx, queue.jobs[0][1][0])
    review_id = queue.jobs[1][1][0]
    assert await machine.run(review_id) == "PUBLISHED"
    review = await store.get(review_id)
    assert review.history == ["INIT", "FINALIZATION", "DECISION", "PUBLISHED"]
    assert review.status_delivered
    assert forge.statuses[0]["state"] == "success"
    assert not forge.comments
    # Queue and worker redelivery do not restart the stage or post twice.
    await receive_event(ctx, queue.jobs[0][1][0])
    await machine.run(review_id)
    assert len(forge.statuses) == 1
    async with store.sessions() as session:
        assert len((await session.scalars(select(Review))).all()) == 1
        assert len((await session.scalars(select(ReviewStage))).all()) == 1


async def test_unhandled_failure_is_passing_and_private(store, forge):
    review = await store.accept(7, 2, "a" * 40, "failure")
    forge.error = RuntimeError("a-private-credential-must-not-be-stored")
    assert await ReviewStateMachine(store, forge).run(review.id) == "FAILED_INTERNAL"
    assert (await store.get(review.id)).error == "RuntimeError"
    assert forge.statuses[-1]["state"] == "success"
    assert forge.comments == []


async def test_new_push_supersedes_and_persists_replacement(store, forge):
    review = await store.accept(7, 2, "b" * 40, "old")
    assert await ReviewStateMachine(store, forge).run(review.id) == "SUPERSEDED"
    pending = await store.pending()
    assert len(pending) == 1
    assert pending[0].head_sha == "a" * 40
    assert forge.statuses[-1]["sha"] == "b" * 40


async def test_shutdown_resumes_from_last_committed_state(store, forge):
    review = await store.accept(7, 2, "a" * 40, "shutdown")
    await store.noop(review.id)
    await store.transition(review.id, "FINALIZATION")
    forge.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await ReviewStateMachine(store, forge).run(review.id)
    assert (await store.get(review.id)).state == "FINALIZATION"
    forge.error = None
    assert await ReviewStateMachine(store, forge).run(review.id) == "PUBLISHED"


async def test_status_failure_retains_durable_retry(store, forge):
    review = await store.accept(7, 2, "a" * 40, "status-failure")
    original = forge.set_commit_status

    async def unavailable(*args):
        raise TimeoutError()

    forge.set_commit_status = unavailable
    with pytest.raises(TimeoutError):
        await ReviewStateMachine(store, forge).run(review.id)
    assert not (await store.get(review.id)).status_delivered
    queue = FakeQueue()
    await recover({"store": store, "redis": queue})
    assert queue.jobs == [("run_review", (review.id,))]
    forge.set_commit_status = original
    await ReviewStateMachine(store, forge).run(review.id)
    assert (await store.get(review.id)).status_delivered


async def test_partial_unique_index_prevents_two_active_reviews(store):
    await store.accept(7, 2, "a" * 40, "unique-1")
    with pytest.raises(IntegrityError):
        async with store.sessions.begin() as session:
            session.add(
                Review(project_id=1, mr_iid=2, head_sha="b" * 40, event_id="unique-2")
            )


async def test_admission_retires_previous_run(store):
    old = await store.accept(7, 2, "a" * 40, "first")
    new = await store.accept(7, 2, "b" * 40, "second")
    old = await store.get(old.id)
    assert old.state == "SUPERSEDED"
    assert old.superseded_by == new.id
    assert (await store.accept(7, 2, "b" * 40, "second")).id == new.id


async def test_cancel_and_invalid_transition(store, forge):
    review = await store.accept(7, 2, "a" * 40, "cancel")
    with pytest.raises(ValueError):
        await store.transition(review.id, "PUBLISHED")
    await store.cancel(7, 2)
    assert await ReviewStateMachine(store, forge).run(review.id) == "CANCELLED"
    assert forge.statuses[-1]["state"] == "success"
