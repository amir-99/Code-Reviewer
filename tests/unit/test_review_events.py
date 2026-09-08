import asyncio
from types import SimpleNamespace

import httpx
import pytest

from reviewer.api.events import stream
from reviewer.config.schema import Settings
from reviewer.main import create_app
from reviewer.telemetry.activity import activity, sink


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
