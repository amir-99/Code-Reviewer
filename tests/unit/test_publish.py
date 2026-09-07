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


async def test_draft_mode_renders_without_writing_to_gitlab(tmp_path):
    b = bundle(tmp_path, 1)
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.in_diff = True
    f.anchor.introduced_by_this_change = True
    forge = FakeForge(b.mr)
    report = await Publisher(forge).publish(
        b, [f], "COMMENT_ONLY", ProjectConfig(), [], "draft"
    )
    assert forge.comments == []
    assert report["mode"] == "draft"
    assert f.claim in report["summary"]
    assert [c["fingerprint"] for c in report["inline"]] == [f.fingerprint]
    # Nothing was posted, so the finding must not claim it was.
    assert f.status != "published"


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
    # A draft is only rendering, so it stays available under silent enforcement.
    report = await Publisher(forge).publish(b, [f], "COMMENT_ONLY", silent, [], "draft")
    assert report["mode"] == "draft" and forge.comments == []


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
