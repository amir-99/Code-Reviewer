import asyncio

import httpx
import pytest

from reviewer.config.schema import ProjectConfig, Settings
from reviewer.main import create_app
from reviewer.publish.comments import CommentConflict, Comments, entry, revision
from reviewer.services.forge.gitlab import Discussion, GitLab, Note, Position
from reviewer.store.models import Review
from tests.account_helpers import signed_in
from tests.conftest import FakeQueue

FP = "1" * 32
FP2 = "2" * 32


def body(key=FP):
    return f"A defect\n\n<sub>AI review · machine-assisted</sub>\n<!-- ai-review:fingerprint={key} -->"


def position():
    return Position(
        base_sha="b" * 40,
        start_sha="b" * 40,
        head_sha="a" * 40,
        old_path="app.py",
        new_path="app.py",
        new_line=1,
    )


@pytest.fixture
async def management(store, forge):
    review = await store.accept(7, 2, "a" * 40, "comments")
    await store.save_snapshot(
        review.id,
        {
            "report": {
                "inline": [
                    {
                        "fingerprint": FP,
                        "body": body(),
                        "file": "app.py",
                        "position": position().model_dump(),
                    },
                    {
                        "fingerprint": FP2,
                        "body": body(FP2),
                        "file": "app.py",
                        "position": position().model_dump(),
                    },
                ],
                "summary": f"Report\n<sub>AI review</sub>\n<!-- ai-review:summary={review.head_sha} -->",
            }
        },
    )
    return Comments(store, forge, review, 7, ProjectConfig())


async def test_edit_publish_and_remove_preserves_human_reply(management, forge):
    draft = await forge.post_draft_note(7, 2, body(), position())
    await management.sync()
    await management.act(FP, "edit", revision(draft.body), "Corrected message")
    assert "Corrected message" in draft.body
    assert "<sub>AI review" in draft.body and FP in draft.body
    await management.act(FP, "publish")
    await management.sync()
    row = management.rows[FP]
    assert row["status"] == "committed" and row["thread_status"] == "open"
    thread = forge.discussions[0]
    thread.notes.append(Note(id=999, body="Human reply", author_id=42))
    await management.act(FP, "resolve")
    assert thread.resolved
    await management.act(FP, "remove")
    assert [n.body for n in thread.notes] == ["Human reply"]
    assert management.rows[FP]["status"] == "removed"
    assert await management.act(FP, "publish") == "skipped"
    assert len(forge.comments) == 1


async def test_draft_published_outside_app_and_summary_controls(management, forge):
    await management.sync()
    summary = await forge.post_draft_note(7, 2, management.rows["summary"]["body"])
    await management.sync()
    await forge.publish_draft_note(7, 2, summary.id)
    await management.sync()
    assert management.rows["summary"]["status"] == "committed"
    assert management.rows["summary"]["thread_status"] == "not_applicable"
    await management.act(
        "summary",
        "edit",
        revision(management.rows["summary"]["body"]),
        "Updated report",
    )
    assert forge.comments[0].body.startswith("Updated report")
    assert await management.act("summary", "resolve") == "skipped"
    await management.act("summary", "remove")
    assert not forge.discussions


async def test_stale_editor_and_marker_injection_rejected(management, forge):
    note = await forge.post_note(7, 2, body())
    old = revision(note.body)
    note.body = "External edit\n" + note.body
    with pytest.raises(CommentConflict, match="changed"):
        await management.act(FP, "edit", old, "Overwrite")
    with pytest.raises(CommentConflict, match="markers"):
        await management.act(FP, "edit", revision(note.body), body(FP2))
    assert note.body.startswith("External edit")


async def test_lost_removal_response_is_reconciled(management, forge, monkeypatch):
    draft = await forge.post_draft_note(7, 2, body(), position())
    original = forge.delete_draft_note

    async def lost(*args):
        await original(*args)
        raise httpx.ReadTimeout("private upstream text")

    monkeypatch.setattr(forge, "delete_draft_note", lost)
    with pytest.raises(httpx.ReadTimeout):
        await management.act(FP, "remove")
    assert management.rows[FP]["intent"] == "remove"
    await management.sync()
    assert management.rows[FP]["status"] == "removed"
    assert await management.act(FP, "publish") == "skipped"
    assert all(n.id != draft.id for n in forge.draft_notes)


async def test_lost_publish_response_does_not_duplicate(management, forge, monkeypatch):
    original = forge.post_note

    async def lost(*args):
        await original(*args)
        raise httpx.ReadTimeout("private upstream text")

    monkeypatch.setattr(forge, "post_note", lost)
    with pytest.raises(httpx.ReadTimeout):
        await management.act("summary", "publish")
    assert await management.act("summary", "publish") == "skipped"
    assert len(forge.comments) == 1


async def test_ambiguous_publish_without_visible_result_never_recreates(
    management, forge, monkeypatch
):
    async def lost(*args):
        raise httpx.ReadTimeout("private")

    monkeypatch.setattr(forge, "post_note", lost)
    with pytest.raises(httpx.ReadTimeout):
        await management.act("summary", "publish")
    with pytest.raises(CommentConflict, match="unknown"):
        await management.act("summary", "publish")


async def test_inline_limits_include_other_threads(management, forge):
    for n in range(5):
        forge.discussions.append(
            Discussion(
                id=str(n), file="app.py", notes=[Note(id=n, body=body(f"{n:032x}"))]
            )
        )
    with pytest.raises(CommentConflict, match="limit"):
        await management.act(FP, "publish")
    assert await management.act("summary", "publish") == "succeeded"


async def test_identity_change_is_not_a_removed_comment(management, forge):
    note = await forge.post_note(7, 2, body())
    await management.sync()
    note.body = "Identity marker removed externally"
    await management.sync()
    assert "identity changed" in management.rows[FP]["conflict"]
    with pytest.raises(CommentConflict):
        await management.act(FP, "remove")


@pytest.fixture
async def owner_api(store, forge, tmp_path):
    config = tmp_path / "projects.json"
    config.write_text('{"defaults":{"enforcement":"advisory"}}')
    app = create_app(
        Settings(_env_file=None, config_path=config), store, FakeQueue(), forge
    )
    headers = await signed_in(app, "user", forge)
    from reviewer.accounts.credentials import Credentials

    refs = await Credentials(store, app.state.settings).pin(app.state.test_account.id)
    review = await store.accept(
        7,
        2,
        "a" * 40,
        "owner-comments",
        owner_user_id=app.state.test_account.id,
        credential_refs=refs,
        principal_id=str(forge.bot_id),
        trigger_source="manual",
    )
    async with store.transaction() as session:
        row = await session.get(Review, review.id)
        row.state = "PUBLISHED"
    forge.owner_user_id = review.owner_user_id
    owner_marker = f"\n<!-- ai-review:owner={review.owner_user_id} -->"
    await store.save_comment(
        review.id, FP, entry(FP, body() + owner_marker, file="app.py")
    )
    await store.save_comment(
        review.id,
        "summary",
        entry(
            "summary",
            f"Report\n<!-- ai-review:summary={review.head_sha} -->" + owner_marker,
        ),
    )
    await forge.post_draft_note(7, 2, body() + owner_marker, position())
    await forge.post_draft_note(7, 2, "Unrelated personal draft")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers=headers,
    ) as client:
        yield app, client, review


async def test_bulk_actions_are_scoped_idempotent_and_include_summary(owner_api, forge):
    app, client, review = owner_api
    url = f"/admin/reviews/{review.id}/comments"
    for _ in range(2):
        response = await client.post(url, json={"action": "publish_all"})
        assert response.status_code == 200, response.text
        assert not any(r["status"] == "failed" for r in response.json()["results"])
    assert len(forge.comments) == 2
    assert [n.body for n in forge.draft_notes] == ["Unrelated personal draft"]
    response = await client.post(url, json={"action": "resolve_all"})
    results = {r["key"]: r["status"] for r in response.json()["results"]}
    assert results == {FP: "succeeded", "summary": "skipped"}
    assert forge.discussions[0].resolved


async def test_bulk_partial_failure_and_retry(owner_api, forge, monkeypatch):
    _, client, review = owner_api
    original = forge.publish_draft_note

    async def fail(*args):
        raise httpx.ReadTimeout("do not expose upstream error")

    monkeypatch.setattr(forge, "publish_draft_note", fail)
    url = f"/admin/reviews/{review.id}/comments"
    response = await client.post(url, json={"action": "publish_all"})
    assert response.status_code == 200
    assert [r["status"] for r in response.json()["results"]] == ["failed", "succeeded"]
    assert "do not expose" not in response.text
    monkeypatch.setattr(forge, "publish_draft_note", original)
    response = await client.post(url, json={"action": "publish_all"})
    assert all(r["status"] != "failed" for r in response.json()["results"])
    assert len(forge.comments) == 2


async def test_ownership_admin_csrf_silent_and_stale_guards(owner_api, forge):
    app, client, review = owner_api
    url = f"/admin/reviews/{review.id}/comments"
    assert (
        await client.post(
            url, json={"action": "publish_all"}, headers={"X-CSRF-Token": "wrong"}
        )
    ).status_code == 403
    forge.mr.head_sha = "b" * 40
    assert (await client.post(url, json={"action": "publish_all"})).status_code == 409
    forge.mr.head_sha = "a" * 40
    app.state.settings.config_path.write_text('{"defaults":{"enforcement":"silent"}}')
    assert (await client.post(url, json={"action": "publish_all"})).status_code == 409
    other = await signed_in(app, "user", forge)
    assert (await client.get(url, headers=other)).status_code == 404
    assert (
        await client.post(url, headers=other, json={"action": "resolve_all"})
    ).status_code == 404
    admin = await signed_in(app, "admin")
    assert (await client.get(url, headers=admin)).status_code == 200
    assert (
        await client.post(url, headers=admin, json={"action": "publish_all"})
    ).status_code == 403
    assert not forge.comments


async def test_concurrent_bulk_delivery_posts_once(owner_api, forge):
    _, client, review = owner_api
    responses = await asyncio.gather(
        *[
            client.post(
                f"/admin/reviews/{review.id}/comments", json={"action": "publish_all"}
            )
            for _ in range(2)
        ]
    )
    assert all(r.status_code == 200 for r in responses)
    assert len(forge.comments) == 2


async def test_gitlab_comment_http_contract():
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(204)

    forge = GitLab(
        "https://gitlab.invalid/base", "secret", transport=httpx.MockTransport(handle)
    )
    await forge.edit_draft_note(7, 2, 3, "Draft")
    await forge.delete_draft_note(7, 2, 3)
    await forge.publish_draft_note(7, 2, 4)
    await forge.edit_note(7, 2, 5, "Posted")
    await forge.delete_note(7, 2, 5)
    assert [(m, p.rsplit("/merge_requests/2/", 1)[1]) for m, p, _ in seen] == [
        ("PUT", "draft_notes/3"),
        ("DELETE", "draft_notes/3"),
        ("PUT", "draft_notes/4/publish"),
        ("PUT", "notes/5"),
        ("DELETE", "notes/5"),
    ]
    assert seen[0][2] == b'{"note":"Draft"}' and seen[3][2] == b'{"body":"Posted"}'
    await forge.close()


async def test_legacy_deleted_draft_is_not_republished(management, store, forge):
    snapshot = await store.snapshot(management.review.id)
    snapshot["report"]["mode"] = "draft"
    await store.save_snapshot(management.review.id, snapshot)
    await management.sync()
    assert all(row["status"] == "removed" for row in management.rows.values())
    assert await management.act("summary", "publish") == "skipped"
    assert not forge.comments


async def test_removed_draft_stays_removed_in_bulk(owner_api, forge):
    _, client, review = owner_api
    url = f"/admin/reviews/{review.id}/comments"
    response = await client.post(url, json={"action": "remove", "key": FP})
    assert response.json()["results"][0]["status"] == "succeeded"
    response = await client.post(url, json={"action": "publish_all"})
    assert response.status_code == 200
    assert len(forge.comments) == 1 and "summary=" in forge.comments[0].body
    rows = {r["key"]: r for r in response.json()["comments"]}
    assert rows[FP]["status"] == "removed"


async def test_revoked_credentials_and_changed_author_cannot_write(owner_api, forge):
    from reviewer.accounts.credentials import Credentials

    app, client, review = owner_api
    url = f"/admin/reviews/{review.id}/comments"
    forge.draft_notes[0].author_id = 1234
    response = await client.post(url, json={"action": "remove", "key": FP})
    assert response.json()["results"][0]["status"] == "failed"
    assert len(forge.draft_notes) == 2
    await Credentials(app.state.store, app.state.settings).remove(
        review.owner_user_id, "gitlab"
    )
    response = await client.post(url, json={"action": "publish_all"})
    assert response.status_code == 409
    assert not forge.comments


async def test_failed_sync_returns_cached_state_without_credentials(
    owner_api, forge, monkeypatch
):
    _, client, review = owner_api
    url = f"/admin/reviews/{review.id}/comments"
    response = await client.get(url)
    assert response.json()["synced"]

    async def unavailable(*args):
        raise httpx.ReadTimeout("private upstream failure")

    monkeypatch.setattr(forge, "list_draft_notes", unavailable)
    response = await client.get(url)
    assert response.status_code == 200
    assert not response.json()["synced"] and not response.json()["can_manage"]
    assert response.json()["comments"][0]["status"] == "drafted"
    assert "private upstream" not in response.text


async def test_sync_redacts_remote_message_credentials(management, forge):
    await forge.post_note(7, 2, body() + "\npassword: abcdefghijklmnop")
    await management.sync()
    assert "abcdefghijklmnop" not in str(management.view())


async def test_external_publication_can_reconcile_after_temporary_absence(
    management, forge
):
    draft = await forge.post_draft_note(7, 2, body(), position())
    await management.sync()
    forge.draft_notes = []
    await management.sync()
    # The next consistent read sees the published version. An inferred absence
    # is different from an owner's durable removal instruction.
    await forge.post_note(7, 2, draft.body)
    await management.sync()
    assert management.rows[FP]["status"] == "committed"


async def test_deleted_receipt_does_not_adopt_a_later_reviews_note(management, forge):
    note = await forge.post_note(7, 2, body())
    await management.sync()
    await forge.delete_note(7, 2, note.id)
    later = await forge.post_note(7, 2, body())
    await management.sync()
    assert management.rows[FP]["status"] == "removed"
    assert await management.act(FP, "remove") == "skipped"
    assert later in forge.discussions[0].notes


async def test_definitively_rejected_publication_can_be_retried(
    management, forge, monkeypatch
):
    original = forge.post_note

    async def forbidden(*args):
        response = httpx.Response(
            403, request=httpx.Request("POST", "https://gitlab.invalid/notes")
        )
        response.raise_for_status()

    monkeypatch.setattr(forge, "post_note", forbidden)
    with pytest.raises(httpx.HTTPStatusError):
        await management.act("summary", "publish")
    assert "intent" not in management.rows["summary"]
    monkeypatch.setattr(forge, "post_note", original)
    assert await management.act("summary", "publish") == "succeeded"
    assert len(forge.comments) == 1


async def test_review_reference_links_include_live_gitlab_for_legacy_runs(
    owner_api, forge
):
    app, client, review = owner_api
    app.state.settings.gitlab_base_url = "https://git.example"
    app.state.settings.jira_base_url = "https://jira.example"
    app.state.settings.confluence_base_url = "https://wiki.example"
    forge.mr.web_url = "https://git.example/team/repo/-/merge_requests/2"
    await app.state.store.save_snapshot(
        review.id,
        {
            "bundle": {
                "issue": {"key": "APP-42"},
                "documents": [
                    {"title": "Requirements", "url": "https://wiki.example/pages/12"}
                ],
            }
        },
    )
    response = await client.get(f"/admin/reviews/{review.id}")
    assert response.status_code == 200
    assert [link["kind"] for link in response.json()["links"]] == ["jira", "confluence"]
    response = await client.get(f"/admin/reviews/{review.id}/comments")
    assert response.status_code == 200
    assert response.json()["links"][0] == {
        "kind": "gitlab",
        "label": "GitLab merge request",
        "url": forge.mr.web_url,
    }
