import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from reviewer.api.events import CLOSING_GRACE, closing, stream
from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.telemetry.activity import activity, close_run, open_run, sink


def connected(store):
    async def never():
        return False

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(store=store)), is_disconnected=never
    )


async def test_authenticated_list_and_sse_replay(store):
    review = await store.accept(7, 2, "a" * 40, "event")
    await store.append_event(review.id, "tool", {"name": "Git", "status": "started"})
    await store.transition(review.id, "CANCELLED")
    app = create_app(settings=Settings(admin_token="operator"), store=store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/admin/reviews/{review.id}/events"
        assert (await client.get(path)).status_code == 401
        client.headers["Authorization"] = "Bearer operator"
        listed = (await client.get("/admin/reviews")).json()["reviews"]
        assert listed[0]["id"] == review.id
        assert listed[0]["project_id"] == 7
        assert (await client.get("/admin/reviews?limit=101")).status_code == 422
        assert (await client.get("/admin/reviews?event_id=event")).json()[
            "id"
        ] == review.id
        assert (await client.get("/admin/reviews?event_id=queued")).json()[
            "state"
        ] == "QUEUED"
        response = await client.get(path, headers={"Last-Event-ID": "1"})
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-store"
        assert "id: 1\n" not in response.text
        assert "id: 2\n" in response.text and "id: 3\n" in response.text
        assert response.text.index("id: 2\n") < response.text.index("id: 3\n")
        assert "event: snapshot" in response.text and "event: complete" in response.text
        for cursor in ["-1", "invalid", str(2**64)]:
            assert (
                await client.get(path, headers={"Last-Event-ID": cursor})
            ).status_code == 400
        assert (await client.get("/admin/reviews/missing/events")).status_code == 404


async def test_supersession_emits_terminal_event_and_duplicates_do_not(store):
    first = await store.accept(7, 2, "a" * 40, "first")
    await store.accept(7, 2, "a" * 40, "first")
    assert len(await store.events(first.id)) == 1
    await store.accept(7, 2, "b" * 40, "second")
    events = await store.events(first.id)
    assert [e["data"]["state"] for e in events] == ["INIT", "SUPERSEDED"]


async def test_activity_nesting_isolation_and_no_payload_leaks():
    class RecordingStore:
        def __init__(self):
            self.rows = []

        async def append_event(self, review_id, kind, data):
            self.rows.append((review_id, kind, data))

    store = RecordingStore()

    @activity("tool", "Read code")
    async def tool(secret):
        await asyncio.sleep(0)
        return secret

    @activity("agent", "purpose")
    async def agent():
        return await tool("secret-code-and-token")

    async def run(review_id):
        token = sink.set((store, review_id))
        try:
            await agent()
        finally:
            sink.reset(token)

    await asyncio.gather(run("one"), run("two"))
    assert "secret-code-and-token" not in str(store.rows)
    for review_id in ("one", "two"):
        rows = [r[2] for r in store.rows if r[0] == review_id]
        assert [r["status"] for r in rows] == [
            "started",
            "started",
            "completed",
            "completed",
        ]
        assert rows[1]["parent_id"] == rows[0]["activity_id"]
        assert rows[0]["parent_id"] is None
    assert sink.get() is None


async def test_observability_failure_does_not_fail_work():
    class BrokenStore:
        async def append_event(self, *args):
            raise RuntimeError("database unavailable")

    @activity("tool", "Read code")
    async def work():
        return 42

    token = sink.set((BrokenStore(), "review"))
    try:
        assert await work() == 42
    finally:
        sink.reset(token)


async def test_stream_drains_more_than_one_page_and_disconnects(store):
    review = await store.accept(7, 2, "a" * 40, "event")
    for _ in range(201):
        await store.append_event(review.id, "tool", {"name": "Read code"})
    await store.transition(review.id, "CANCELLED")

    async def connected():
        return False

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(store=store)),
        is_disconnected=connected,
    )
    frames = [frame async for frame in stream(request, review.id, 0)]
    assert sum("event: activity" in frame for frame in frames) == 203
    assert "event: complete" in frames[-1]

    async def disconnected():
        return True

    request.is_disconnected = disconnected
    frames = [frame async for frame in stream(request, review.id, 0)]
    assert len(frames) == 2


async def test_failed_activity_emits_safe_failure():
    rows = []

    class RecordingStore:
        async def append_event(self, *args):
            rows.append(args)

    @activity("tool", "Read code")
    async def work():
        raise ValueError("private upstream failure")

    token = sink.set((RecordingStore(), "review"))
    try:
        with pytest.raises(ValueError):
            await work()
    finally:
        sink.reset(token)
    assert rows[-1][2]["status"] == "failed"
    assert "private upstream failure" not in str(rows)


async def test_concurrent_activity_has_unique_ordered_sequences(store):
    review = await store.accept(7, 2, "a" * 40, "concurrent")
    await asyncio.gather(
        *(store.append_event(review.id, "tool", {"name": "Git"}) for _ in range(20))
    )
    rows = await store.events(review.id)
    assert [r["id"] for r in rows] == list(range(1, 22))


async def test_terminal_state_is_not_the_end_of_the_run(store):
    """The commit status is delivered after the terminal transition.

    Completing the stream on the transition alone reports the review finished
    while its worker is still going, and freezes its snapshot on a status that
    had not been delivered yet.
    """
    review = await store.accept(7, 2, "a" * 40, "bracket")
    await open_run(store, review.id)
    await store.transition(review.id, "CANCELLED")

    assert await closing(store, await store.get(review.id)) is True
    await store.mark_status(review.id)
    await close_run(store, review.id)
    assert await closing(store, await store.get(review.id)) is False
    assert (await store.run_state(review.id)) == "cancelled"

    frames = [frame async for frame in stream(connected(store), review.id, 0)]
    # INIT, the run opening, CANCELLED and the run closing all reach the client.
    assert sum("event: activity" in frame for frame in frames) == 4
    assert "event: complete" in frames[-1]
    assert '"status_delivered": true' in frames[-2]


async def test_an_abandoned_run_does_not_hold_the_stream_open(store):
    """A worker can die between the transition and its closing marker."""
    review = await store.accept(7, 2, "a" * 40, "abandoned")
    await open_run(store, review.id)
    await store.transition(review.id, "CANCELLED")
    stale = datetime.now(UTC) - timedelta(seconds=CLOSING_GRACE + 1)
    assert (
        await closing(store, SimpleNamespace(id=review.id, finished_at=stale)) is False
    )


async def test_a_review_with_no_run_marker_completes_at_once(store):
    """Supersession and cancellation are recorded without a worker run."""
    review = await store.accept(7, 2, "a" * 40, "unrun")
    await store.transition(review.id, "SUPERSEDED")
    assert await closing(store, await store.get(review.id)) is False


async def test_snapshot_reports_the_events_it_already_reflects(store):
    """Replayed state events must not move a header the snapshot has walked."""
    review = await store.accept(7, 2, "a" * 40, "cursor")
    await store.transition(review.id, "CONTEXT_COLLECTION")
    await store.transition(review.id, "CANCELLED")
    frames = [frame async for frame in stream(connected(store), review.id, 0)]
    opening = next(frame for frame in frames if "event: snapshot" in frame)
    assert '"sequence": 3' in opening
    assert '"id: 3' not in opening


async def test_closing_a_run_never_fails_the_review(store):
    class Broken:
        async def get(self, review_id):
            raise RuntimeError("database unavailable")

        async def append_event(self, *args):
            raise RuntimeError("database unavailable")

    await open_run(Broken(), "review")
    await close_run(Broken(), "review")


async def test_activity_durations_and_logs_exclude_private_arguments(store):
    from structlog.testing import capture_logs

    review = await store.accept(7, 2, "a" * 40, "timed")

    @activity("tool", "Read code")
    async def work(private):
        return private

    token = sink.set((store, review.id))
    try:
        with capture_logs() as logs:
            assert await work("private-code") == "private-code"
    finally:
        sink.reset(token)
    events = await store.events(review.id)
    assert events[-1]["data"]["duration_ms"] >= 0
    assert "duration_ms" not in events[-2]["data"]
    assert any(row["event"] == "activity_write_finished" for row in logs)
    assert "private-code" not in str(logs)


async def test_hung_optional_telemetry_is_cancelled_without_failing_work(monkeypatch):
    from importlib import import_module

    module = import_module("reviewer.telemetry.activity")
    monkeypatch.setattr(module, "EVENT_TIMEOUT_S", 0.01)
    cancelled = []

    class HungStore:
        async def append_event(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    @activity("tool", "Read code")
    async def work():
        return 42

    token = sink.set((HungStore(), "review"))
    try:
        async with asyncio.timeout(1):
            assert await work() == 42
    finally:
        sink.reset(token)
    assert len(cancelled) == 2


async def test_cancellation_records_duration_and_propagates():
    events = []
    entered = asyncio.Event()

    class RecordingStore:
        async def append_event(self, review_id, kind, data):
            events.append(data)

    @activity("tool", "Read code")
    async def work():
        entered.set()
        await asyncio.Event().wait()

    token = sink.set((RecordingStore(), "review"))
    try:
        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        sink.reset(token)
    assert events[-1]["status"] == "cancelled"
    assert events[-1]["duration_ms"] >= 0
