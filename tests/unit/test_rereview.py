from hashlib import sha256

from test_findings import finding

from reviewer.context.models import ChangedFile, Hunk
from reviewer.findings.dedup import deduplicate
from reviewer.publish.rereview import adopt_identities, follow, full_review, reanchor


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


def published(line, fingerprint="p" * 32, text="x=1"):
    f = finding(line)
    f.fingerprint = fingerprint
    f.anchor.context_hash = sha256(text.encode()).hexdigest()
    return f


def fresh(line, fingerprint="n" * 32, category="correctness"):
    f = finding(line)
    f.fingerprint = fingerprint
    f.category = category
    f.claim = "Reworded description of the same defect"
    return f


def test_reworded_claim_at_a_moved_location_keeps_its_published_identity(tmp_path):
    (tmp_path / "f0.py").write_text("a=0\nb=0\nx=1\n")
    new = fresh(3)
    adopt_identities([new], [published(1)], tmp_path)
    assert new.fingerprint == "p" * 32


def test_changed_snippet_is_followed_through_the_diff(tmp_path):
    (tmp_path / "f0.py").write_text("\n".join(f"l{n}" for n in range(30)))
    # Five lines inserted before line 10; the old line 20 is now line 25.
    delta = [
        ChangedFile(
            path="f0.py",
            change_type="modified",
            hunks=[
                Hunk(old_start=9, old_lines=0, new_start=10, new_lines=5, header="")
            ],
        )
    ]
    assert follow(20, delta[0]) == 25
    assert follow(5, delta[0]) == 5
    new = fresh(25)
    adopt_identities([new], [published(20, text="gone")], tmp_path, delta)
    assert new.fingerprint == "p" * 32


def test_other_category_or_distant_claim_keeps_a_new_identity(tmp_path):
    (tmp_path / "f0.py").write_text("\n".join(f"l{n}" for n in range(60)))
    other = fresh(20, category="security")
    distant = fresh(50, fingerprint="m" * 32)
    adopt_identities([other, distant], [published(20, text="gone")], tmp_path)
    assert other.fingerprint == "n" * 32
    assert distant.fingerprint == "m" * 32


def test_one_published_identity_is_adopted_once(tmp_path):
    (tmp_path / "f0.py").write_text("x=1\n")
    first, second = fresh(1), fresh(1, fingerprint="m" * 32)
    adopt_identities([first, second], [published(1)], tmp_path)
    assert [first.fingerprint, second.fingerprint] == ["p" * 32, "m" * 32]


def test_carried_identity_survives_an_overlapping_merge():
    carried, new = published(10), fresh(10)
    merged = deduplicate([carried, new])
    assert [f.fingerprint for f in merged] == ["p" * 32]
