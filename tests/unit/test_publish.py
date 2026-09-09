import pytest
from test_findings import finding
from test_stages import bundle

from reviewer.config.schema import ProjectConfig
from reviewer.findings.noise import select
from reviewer.publish.publisher import Publisher
from reviewer.publish.renderer import render
from reviewer.services.forge.gitlab import FakeForge


async def test_idempotency_caps_and_bad_position_summary(tmp_path):
    b = bundle(tmp_path, 1)
    forge = FakeForge(b.mr)
    config = ProjectConfig()
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = True
    f.anchor.introduced_by_this_change = True
    pub = Publisher(forge)
    await pub.publish(b, [f], "COMMENT_ONLY", config, [])
    await pub.publish(b, [f], "COMMENT_ONLY", config, [])
    assert len(forge.comments) == 2
    forged = FakeForge(b.mr)
    forged.bad_position = True
    await Publisher(forged).publish(b, [f], "COMMENT_ONLY", config, [])
    assert len(forged.comments) == 1 and f.claim in forged.comments[0].body
    many = []
    for n in range(60):
        item = f.model_copy(deep=True)
        item.fingerprint = f"{n:032x}"
        item.anchor.file = f"f{n // 10}.py"
        many.append(item)
    inline, _, overflow = select(many)
    assert len(inline) == 15 and sum(overflow.values()) == 45
    assert (
        max(
            sum(x.anchor.file == p for x in inline)
            for p in {x.anchor.file for x in inline}
        )
        <= 5
    )


def test_render_golden():
    from pathlib import Path

    f = finding()
    f.severity_final = "REQUIRED"
    assert render(f) == Path("tests/fixtures/comment.md").read_text().rstrip("\n")


def test_added_removed_and_context_positions():
    from reviewer.context.models import ChangedFile, DiffLine
    from reviewer.publish.publisher import position_for_line
    from reviewer.services.forge.gitlab import DiffRefs

    file = ChangedFile(path="new.py", old_path="old.py", change_type="renamed")
    refs = DiffRefs(base_sha="a", start_sha="b", head_sha="c")
    for kind, old, new in [("added", None, 3), ("removed", 2, None), ("context", 2, 3)]:
        result = position_for_line(
            file, DiffLine(text="x", kind=kind, old_line=old, new_line=new), refs
        ).model_dump(exclude_none=True)
        assert ("old_line" in result) == (old is not None)
        assert ("new_line" in result) == (new is not None)


async def test_draft_mode_queues_gitlab_draft_notes(tmp_path):
    b = bundle(tmp_path, 1)
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = True
    f.anchor.introduced_by_this_change = True
    forge = FakeForge(b.mr)
    report = await Publisher(forge).publish(
        b, [f], "COMMENT_ONLY", ProjectConfig(), [], "draft"
    )
    # Pending comments on the merge request, not notes: nothing was published.
    assert forge.comments == [] and forge.discussions == []
    assert [n.file for n in forge.draft_notes] == [f.anchor.file, None]
    assert f.fingerprint in forge.draft_notes[0].body
    assert b.code.head_sha in forge.draft_notes[1].body
    assert report["mode"] == "draft"
    assert f.claim in report["summary"]
    assert [c["fingerprint"] for c in report["inline"]] == [f.fingerprint]
    # Nothing was posted, so the finding must not claim it was.
    assert f.status != "published"
    # A second drafted run finds its own drafts and repeats neither.
    report = await Publisher(forge).publish(
        b, [f], "COMMENT_ONLY", ProjectConfig(), [], "draft"
    )
    assert len(forge.draft_notes) == 2 and report["inline"] == []
    # An unpositionable anchor is summarized in draft mode exactly as applied.
    forged = FakeForge(b.mr)
    forged.bad_position = True
    report = await Publisher(forged).publish(
        b, [f], "COMMENT_ONLY", ProjectConfig(), [], "draft"
    )
    assert report["inline"] == [] and len(forged.draft_notes) == 1
    assert f.claim in forged.draft_notes[0].body


async def test_report_mode_none_and_silent_enforcement_publish_nothing(tmp_path):
    b = bundle(tmp_path, 1)
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = True
    f.anchor.introduced_by_this_change = True
    forge = FakeForge(b.mr)
    assert (
        await Publisher(forge).publish(
            b, [f], "COMMENT_ONLY", ProjectConfig(), [], "none"
        )
        is None
    )
    assert forge.comments == []
    # Silent is an operator setting: a per-run override cannot introduce writes.
    silent = ProjectConfig(enforcement="silent")
    assert (
        await Publisher(forge).publish(b, [f], "COMMENT_ONLY", silent, [], "applied")
        is None
    )
    assert forge.comments == []
    # Silent writes nothing at all, so a drafted run under it renders the report
    # for the operator and leaves not even a draft note on the merge request.
    report = await Publisher(forge).publish(b, [f], "COMMENT_ONLY", silent, [], "draft")
    assert report["mode"] == "draft" and f.claim in report["summary"]
    assert forge.comments == [] and forge.draft_notes == []


async def test_applied_mode_posts_and_returns_the_same_report(tmp_path):
    b = bundle(tmp_path, 1)
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = True
    f.anchor.introduced_by_this_change = True
    forge = FakeForge(b.mr)
    report = await Publisher(forge).publish(
        b, [f], "COMMENT_ONLY", ProjectConfig(), [], "applied"
    )
    assert report["mode"] == "applied" and f.status == "published"
    # One inline discussion plus the summary note.
    assert len(forge.comments) == 2
    assert report["inline"][0]["body"] == forge.comments[0].body
    assert report["summary"] == forge.comments[1].body


async def test_draft_note_requests_match_the_gitlab_api():
    """The draft-notes wire format, which no fake can verify for us."""
    import httpx

    from reviewer.services.forge.gitlab import GitLab, Position, PositionError

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "author_id": 900,
                        "note": "pending <!-- ai-review:fingerprint=%s -->"
                        % ("a" * 32),
                        "discussion_id": None,
                        "resolve_discussion": False,
                        "position": {"new_path": "f0.py", "new_line": 3},
                    }
                ],
            )
        if b'"position"' in request.read():
            return httpx.Response(400, json={"message": "Note position is invalid"})
        return httpx.Response(
            201,
            json={
                "id": 12,
                "author_id": 900,
                "note": "reply",
                "discussion_id": "d1",
                "resolve_discussion": True,
                "position": None,
            },
        )

    forge = GitLab("https://gitlab.internal", "token", httpx.MockTransport(handler))
    drafted = await forge.list_draft_notes(7, 2)
    assert [(n.id, n.file, n.author_id) for n in drafted] == [(11, "f0.py", 900)]
    note = await forge.post_draft_note(
        7, 2, "reply", in_reply_to_discussion_id="d1", resolve_discussion=True
    )
    assert note.discussion_id == "d1" and note.resolve_discussion
    import json

    assert json.loads(seen[-1][2]) == {
        "note": "reply",
        "in_reply_to_discussion_id": "d1",
        "resolve_discussion": True,
    }
    position = Position(
        base_sha="b" * 40,
        start_sha="b" * 40,
        head_sha="a" * 40,
        old_path="f0.py",
        new_path="f0.py",
        new_line=3,
    )
    with pytest.raises(PositionError):
        await forge.post_draft_note(7, 2, "inline", position)
    assert {path for _, path, _ in seen} == {
        "/api/v4/projects/7/merge_requests/2/draft_notes"
    }
    await forge.close()


async def test_advisory_impact_in_inline_and_summary(tmp_path):
    f = finding()
    f.impact_level = "HIGH"
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = f.anchor.introduced_by_this_change = True
    assert "| Impact level (advisory) | HIGH |" in render(f)
    b = bundle(tmp_path, 1)
    forge = FakeForge(b.mr)
    report = await Publisher(forge).publish(b, [f], "COMMENT_ONLY", ProjectConfig(), [])
    assert "Impact (advisory)" in report["summary"]
    assert "| HIGH | correctness |" in report["summary"]
