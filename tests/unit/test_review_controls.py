"""Stop, rerun and retry: who may use each, what each one does, and why not."""

import json

import httpx
import pytest

from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.orchestrator.states import STOPPED_BY_OWNER
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
        "stop": {"allowed": False, "reason": "The review has finished"},
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
        assert controls["stop"]["allowed"] is False
        assert (
            controls["rerun"]["reason"] == "Only the review's owner can run or stop it"
        )
        for action in ("retry", "rerun", "replay", "stop"):
            response = await admin.post(f"/admin/reviews/{review.id}/{action}")
            assert response.status_code == 403
    assert app.state.queue.jobs == []


async def test_owner_stops_a_running_review_once(api, store):
    app, client, failed = api
    review = await store.accept(
        7,
        2,
        "a" * 40,
        "running-run",
        owner_user_id=app.state.test_account.id,
        trigger_source="manual",
    )
    await store.transition(review.id, "CONTEXT_COLLECTION")
    controls = (await client.get(f"/admin/reviews/{review.id}")).json()["controls"]
    assert controls["stop"] == {"allowed": True, "reason": None}
    response = await client.post(f"/admin/reviews/{review.id}/stop")
    assert response.status_code == 200, response.text
    stopped = await store.get(review.id)
    assert stopped.state == "CANCELLED"
    assert stopped.error == STOPPED_BY_OWNER
    detail = (await client.get(f"/admin/reviews/{review.id}")).json()
    assert detail["controls"]["stop"]["allowed"] is False
    assert detail["reasons"][0]["message"] == STOPPED_BY_OWNER
    again = await client.post(f"/admin/reviews/{review.id}/stop")
    assert again.status_code == 409
    assert again.json()["detail"] == "The review has finished"
    requested = [e for e in await store.events(review.id) if e["kind"] == "request"]
    assert requested[-1]["data"]["name"].startswith("Stop requested by")


async def test_a_review_publishing_its_report_cannot_be_stopped(api, store):
    app, client, _ = api
    review = await store.accept(
        7,
        2,
        "a" * 40,
        "publishing-run",
        owner_user_id=app.state.test_account.id,
        trigger_source="manual",
    )
    for state in ("CONTEXT_COLLECTION", "FINALIZATION", "DECISION"):
        await store.transition(review.id, state)
    controls = (await client.get(f"/admin/reviews/{review.id}")).json()["controls"]
    assert controls["stop"]["reason"] == "The review is publishing its report"
    response = await client.post(f"/admin/reviews/{review.id}/stop")
    assert response.status_code == 409
    assert (await store.get(review.id)).state == "DECISION"
    # The store refuses it under its own lock too, whatever the caller checked.
    assert await store.stop(review.id) is None


async def test_failed_review_explains_itself(api, store):
    app, client, review = api
    await store.save_snapshot(
        review.id,
        {"bundle": {"degradations": ["static_unavailable", "excluded:a.go"]}},
    )
    reasons = (await client.get(f"/admin/reviews/{review.id}")).json()["reasons"]
    assert [r["code"] for r in reasons] == ["error", "static_unavailable", "excluded"]
    assert reasons[0]["message"] == "gateway unavailable"
    assert reasons[2]["message"] == "1 changed file(s) were excluded from review"


def test_partial_reasons_name_their_cause_and_never_invent_one():
    from types import SimpleNamespace

    from reviewer.orchestrator.reasons import failure_reason, reasons

    published = SimpleNamespace(state="PUBLISHED", partial=True, error=None)
    found = reasons(published, ["partial:defect_review", "budget_exhausted", "partial"])
    assert [r["code"] for r in found] == ["partial:defect_review", "budget_exhausted"]
    assert all(r["partial"] for r in found)
    assert found[0]["message"] == "The defect review stage did not cover every unit"
    # Partial with no recorded cause says what is known, not a guess.
    [unknown] = reasons(published, ["partial"])
    assert unknown["code"] == "partial" and "milestone" in unknown["message"]
    superseded = SimpleNamespace(state="SUPERSEDED", partial=False, error=None)
    assert reasons(superseded)[0]["code"] == "superseded"
    private = RuntimeError("token=glpat-secret at https://internal")
    reason = failure_reason(private, "DEFECT_REVIEW")
    assert reason == (
        "An internal error stopped the review during defect review (RuntimeError)"
    )


async def test_worker_cancels_a_stopped_run_and_delivers_its_status(store, monkeypatch):
    import asyncio

    from reviewer import worker

    monkeypatch.setattr(worker, "STOP_POLL_S", 0.01)
    review = await store.accept(7, 2, "a" * 40, "watched")
    await store.transition(review.id, "CONTEXT_COLLECTION")
    started, cancelled = asyncio.Event(), asyncio.Event()

    class Machine:
        calls = 0

        async def run(self, review_id):
            self.calls += 1
            if self.calls == 1:
                started.set()
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return (await store.get(review_id)).state

    machine = Machine()
    running = asyncio.create_task(worker.until_stopped(store, review.id, machine))
    await started.wait()
    await store.stop(review.id)
    assert await asyncio.wait_for(running, 5) == "CANCELLED"
    assert cancelled.is_set()
    # The second run is the terminal one that delivers the commit status.
    assert machine.calls == 2


async def test_worker_leaves_a_run_alone_that_the_pipeline_cancelled(
    store, monkeypatch
):
    import asyncio

    from reviewer import worker

    monkeypatch.setattr(worker, "STOP_POLL_S", 0.01)
    review = await store.accept(7, 2, "a" * 40, "closed-mr")

    class Machine:
        calls = 0

        async def run(self, review_id):
            self.calls += 1
            # A closed merge request: the pipeline's own cancellation, still
            # delivering its status when the watcher looks.
            await store.transition(review_id, "CANCELLED")
            await asyncio.sleep(0.05)
            return "CANCELLED"

    machine = Machine()
    assert await worker.until_stopped(store, review.id, machine) == "CANCELLED"
    assert machine.calls == 1
