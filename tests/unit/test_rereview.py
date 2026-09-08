from hashlib import sha256

from test_findings import finding

from reviewer.publish.rereview import full_review, reanchor


async def test_reanchor_follows_a_moved_snippet(tmp_path):
    f = finding()
    f.anchor.context_hash = sha256(b"x=1").hexdigest()
    (tmp_path / "f0.py").write_text("\nx=1\n")
    state, moved = reanchor(f, tmp_path)
    assert state == "moved" and moved.anchor.line_start == 2
    (tmp_path / "f0.py").write_text("y=2\n")
    assert reanchor(f, tmp_path) == ("invalidated", None)


def test_full_review_triggers():
    prev = {"partial": False, "config_hash": "a", "prompt_hash": "b"}
    assert not full_review(prev, "a", "b", 1)
    assert full_review(prev, "a", "b", 51)
    assert full_review(prev, "c", "b", 0)
