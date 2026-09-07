from test_findings import finding
from test_stages import bundle

from reviewer.publish.commands import command
from reviewer.publish.renderer import render
from reviewer.services.forge.gitlab import FakeForge, Position


async def test_only_developers_can_dismiss_and_feedback_is_idempotent(store, tmp_path):
    b = bundle(tmp_path, 1)
    b.mr.project_id = 7
    b.mr.iid = 2
    forge = FakeForge(b.mr)
    review = await store.accept(7, 2, "a" * 40, "commands")
    f = finding()
    f.severity_final = "REQUIRED"
    await store.save_findings(review, [f])
    discussion = await forge.post_inline_discussion(
        7,
        2,
        render(f),
        Position(
            base_sha="b" * 40,
            start_sha="b" * 40,
            head_sha="a" * 40,
            old_path="f0.py",
            new_path="f0.py",
            new_line=1,
        ),
    )
    ctx = {"forge": forge, "store": store}
    assert (
        await command(ctx, 7, 2, 10, "/ai dismiss incorrect", discussion.id)
        == "forbidden"
    )
    forge.roles[10] = 30
    assert await command(ctx, 7, 2, 10, "/ai explain", discussion.id) == "done"
    assert (
        await command(ctx, 7, 2, 10, "/ai dismiss incorrect", discussion.id) == "done"
    )
    assert forge.discussions[0].resolved
