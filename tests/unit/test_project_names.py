from reviewer.api.webhooks import event_job
from reviewer.store.models import Project


async def test_project_metadata_refresh_preserves_policy_and_missing_names(store):
    review = await store.accept(7, 2, "a" * 40, "named")
    async with store.transaction() as session:
        project = await session.get(Project, review.project_id)
        project.enforcement = "gating"
        project.config_json = {"custom": True}
    await store.provision([7], name="Old", path_with_namespace="group/old")
    await store.provision([7], name="New", path_with_namespace="group/new")
    await store.provision([7])
    async with store.sessions() as session:
        project = await session.get(Project, review.project_id)
        assert project.enforcement == "gating"
        assert project.config_json == {"custom": True}
    assert await store.project_identity(review) == {
        "project_name": "New",
        "project_path": "group/new",
    }
    recent = (await store.recent())[0]
    assert recent["project_name"] == "New"
    assert recent["project_path"] == "group/new"
    assert recent["project_id"] == 7


def test_webhook_carries_project_identity():
    job = event_job(
        {
            "object_kind": "merge_request",
            "project": {
                "id": 7,
                "name": "Project",
                "path_with_namespace": "group/project",
            },
            "object_attributes": {
                "action": "open",
                "iid": 2,
                "last_commit": {"id": "a" * 40},
            },
        },
        "named",
    )
    assert job.project_name == "Project"
    assert job.project_path == "group/project"
