from hashlib import sha256

from test_findings import finding
from test_stages import bundle

from reviewer.publish.publisher import existing
from reviewer.publish.rereview import full_review, reanchor, resolve_fixed
from reviewer.services.forge.gitlab import FakeForge, Position


async def test_fixed_resolves_moved_carries_and_unrelated_preserves(tmp_path):
    f = finding()
    f.anchor.context_hash = sha256(b"x=1").hexdigest()
    (tmp_path / "f0.py").write_text("\nx=1\n")
    state, moved = reanchor(f, tmp_path)
    assert state == "moved" and moved.anchor.line_start == 2
    b = bundle(tmp_path, 1)
    forge = FakeForge(b.mr)
    from reviewer.publish.renderer import render

    f.severity_final = "REQUIRED"
    await forge.post_inline_discussion(
        1,
        1,
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
    discussions, _ = await existing(forge, 1, 1)
    assert not await resolve_fixed(
        forge, 1, 1, [f], [], discussions, True, {"unrelated.py"}
    )
    assert not await resolve_fixed(forge, 1, 1, [f], [], discussions, False, {"f0.py"})
    assert await resolve_fixed(forge, 1, 1, [f], [], discussions, True, {"f0.py"})
    assert forge.discussions[0].resolved


def test_full_review_triggers():
    prev = {"partial": False, "config_hash": "a", "prompt_hash": "b"}
    assert not full_review(prev, "a", "b", 1)
    assert full_review(prev, "a", "b", 51)
    assert full_review(prev, "c", "b", 0)
