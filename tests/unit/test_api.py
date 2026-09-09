import httpx
import pytest
from conftest import FakeQueue
from pydantic import ValidationError

from reviewer.api.webhooks import event_job
from reviewer.config.schema import ProjectConfig, Settings
from reviewer.main import create_app


@pytest.mark.parametrize(
    "action,oldrev,draft,expected",
    [
        ("open", None, False, True),
        ("reopen", None, False, True),
        ("ready", None, False, True),
        ("open", None, True, False),
        ("update", None, False, False),
        ("update", "a" * 40, False, False),
        ("update", "b" * 40, False, True),
        ("close", None, False, True),
    ],
)
def test_trigger_matrix(action, oldrev, draft, expected):
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 7},
        "object_attributes": {
            "iid": 2,
            "action": action,
            "draft": draft,
            "oldrev": oldrev,
            "last_commit": {"id": "a" * 40},
        },
    }
    assert (event_job(payload, "event") is not None) == expected


async def test_auth_and_input_limits(store):
    queue = FakeQueue()
    app = create_app(
        Settings(webhook_secrets={7: "secret"}, admin_token="admin", milestone="M0"),
        store,
        queue,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/webhooks/gitlab", json={"project": {"id": 7}})
        ).status_code == 401
        assert (await client.post("/webhooks/gitlab", content="bad")).status_code == 400
        assert (
            await client.post("/webhooks/gitlab", content="x" * 1_048_577)
        ).status_code == 413
        assert (await client.get("/admin/reviews/missing")).status_code == 401
        assert (
            await client.get(
                "/admin/reviews/missing", headers={"Authorization": "Bearer admin"}
            )
        ).status_code == 404
        assert (await client.get("/health/live")).json()["ai_analysis"] is False
    assert queue.jobs == []


def test_unimplemented_milestones_and_blocking_are_rejected():
    with pytest.raises(ValidationError):
        ProjectConfig(enforcement="blocking")
    with pytest.raises(ValidationError):
        Settings(milestone="M10")


async def test_inspect_returns_findings_and_drafted_report(store):
    """The trigger cannot carry results, so reading them back must work."""
    import sys

    sys.path.insert(0, "tests/unit")
    from test_findings import finding

    review = await store.accept(7, 2, "a" * 40, "event-1")
    f = finding()
    f.severity_final = "REQUIRED"
    f.status = "verified"
    await store.save_findings(review, [f])
    await store.save_snapshot(
        review.id,
        {
            "report": {"mode": "draft", "summary": "# drafted", "inline": []},
            "recheck": {"mode": "draft", "head_sha": "b" * 40, "verdicts": []},
        },
    )
    app = create_app(
        Settings(webhook_secrets={7: "secret"}, admin_token="admin", milestone="M0"),
        store,
        FakeQueue(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        auth = {"Authorization": "Bearer admin"}
        body = (await client.get(f"/admin/reviews/{review.id}", headers=auth)).json()
        assert [x["claim"] for x in body["findings"]] == [f.claim]
        assert body["findings"][0]["severity"] == "REQUIRED"
        assert body["findings"][0]["file"] == f.anchor.file
        assert body["report"]["summary"] == "# drafted"
        assert body["recheck"]["head_sha"] == "b" * 40
        # The event id from a trigger response resolves to the same review.
        found = (
            await client.get("/admin/reviews?event_id=event-1", headers=auth)
        ).json()
        assert found["id"] == review.id and len(found["findings"]) == 1
        queued = (await client.get("/admin/reviews?event_id=nope", headers=auth)).json()
        assert queued["state"] == "QUEUED" and queued["findings"] == []


async def test_recheck_endpoint_enqueues_for_the_reviewed_merge_request(store):
    """What the dashboard's recheck button calls: a job, no review of its own."""
    review = await store.accept(7, 2, "a" * 40, "event-1")
    queue = FakeQueue()
    app = create_app(
        Settings(webhook_secrets={7: "secret"}, admin_token="admin", milestone="M0"),
        store,
        queue,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        auth = {"Authorization": "Bearer admin"}
        response = await client.post(
            f"/admin/reviews/{review.id}/recheck", headers=auth
        )
        assert response.status_code == 200 and response.json()["accepted"] is True
        assert queue.jobs == [("recheck_review", (7, 2))]
        assert (
            await client.post("/admin/reviews/missing/recheck", headers=auth)
        ).status_code == 404
        assert (
            await client.post(f"/admin/reviews/{review.id}/recheck")
        ).status_code == 401


async def test_inspect_reports_spend_against_the_ceiling_the_run_announced(store):
    """Cost is answerable while the review is still running, not only after it."""
    from reviewer.store.models import LLMCall

    review = await store.accept(7, 2, "a" * 40, "spend-1")
    await store.append_event(review.id, "budget", {"token_ceiling": 120000})
    async with store.sessions.begin() as session:
        for stage, model, cost in (
            ("correctness", "vendor/strong", 0.02),
            ("correctness", "vendor/strong", 0.03),
            ("line_review", "vendor/cheap", None),
        ):
            session.add(
                LLMCall(
                    review_id=review.id,
                    stage=stage,
                    model=model,
                    prompt_version="1.0.0",
                    prompt_hash="h",
                    prompt_blob_ref="p",
                    response_blob_ref="r",
                    tokens_in=1000,
                    tokens_out=100,
                    latency_ms=10,
                    cost=cost,
                    outcome="success",
                )
            )
    app = create_app(
        Settings(webhook_secrets={7: "secret"}, admin_token="admin", milestone="M0"),
        store,
        FakeQueue(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        body = (
            await client.get(
                f"/admin/reviews/{review.id}",
                headers={"Authorization": "Bearer admin"},
            )
        ).json()
    spend = body["spend"]
    assert spend["calls"] == 3
    assert spend["tokens"] == 3300
    assert spend["cost"] == pytest.approx(0.05)
    # The ceiling comes from what the run announced, not from re-reading config
    # the review may never have used.
    assert spend["token_ceiling"] == 120000
    assert spend["unpriced_calls"] == 1
    assert [row["role"] for row in spend["roles"]] == ["correctness", "line_review"]
