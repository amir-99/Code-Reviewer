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
