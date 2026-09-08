from hashlib import sha256

from conftest import FakeQueue
from sqlalchemy import select
from test_findings import finding
from test_stages import bundle

from reviewer.context.redaction import Redactor
from reviewer.findings.models import Evidence, RecheckResult
from reviewer.publish.commands import command
from reviewer.publish.recheck import collect, evaluate, publish
from reviewer.publish.renderer import render
from reviewer.services.forge.gitlab import FakeForge, Position
from reviewer.store.models import FindingOutcome, PublishedComment

HEAD = "c" * 40


class Judge:
    """A recheck judge that answers with one fixed verdict."""

    def __init__(self, result):
        self.result, self.calls = result, 0

    async def complete(self, **kwargs):
        self.calls += 1
        return self.result


async def opened(tmp_path, source="x=1\n"):
    """A merge request carrying one published finding on `f0.py`."""
    (tmp_path / "f0.py").write_text(source)
    b = bundle(tmp_path, 1)
    b.mr.project_id, b.mr.iid = 7, 2
    forge = FakeForge(b.mr)
    f = finding()
    f.severity_final = "REQUIRED"
    f.anchor.context_hash = sha256(b"x=1").hexdigest()
    await forge.post_inline_discussion(
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
    return forge, f


async def test_untouched_file_answers_still_open_and_is_posted_once(tmp_path):
    forge, f = await opened(tmp_path)
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(threads, root=tmp_path, touched={"other.py"})
    assert [v.verdict for v in verdicts] == ["not_fixed"]
    posted = await publish(forge, None, 7, 2, 1, verdicts, HEAD)
    assert "Still open" in posted[0]["body"] and not posted[0]["resolved"]
    assert not forge.discussions[0].resolved
    # The reply is the record that this head was answered: a redelivered job or
    # a second manual recheck finds nothing left to do.
    assert await collect(forge, None, 7, 2, [f], HEAD) == []
    assert len(forge.replies) == 1


async def test_absent_from_complete_review_resolves_and_records_outcome(
    store, tmp_path
):
    forge, f = await opened(tmp_path)
    review = await store.accept(7, 2, "a" * 40, "recheck-fixed")
    await store.save_findings(review, [f])
    await store.record_comment(f.id, forge.discussions[0])
    # No findings supplied: the thread's finding is recovered from the store.
    threads = await collect(forge, store, 7, 2, [], HEAD)
    verdicts = await evaluate(
        threads, root=tmp_path, touched={"f0.py"}, absent={f.fingerprint}
    )
    await publish(forge, store, 7, 2, review.project_id, verdicts, HEAD)
    assert verdicts[0].verdict == "fixed" and not verdicts[0].judged
    assert forge.discussions[0].resolved
    async with store.sessions() as session:
        assert (await session.get(PublishedComment, f.id)).resolved_at is not None
        outcomes = (await session.scalars(select(FindingOutcome))).all()
    assert [(o.fingerprint, o.outcome) for o in outcomes] == [
        (f.fingerprint, "actioned")
    ]


async def test_judgement_reports_what_changed_and_resolves(tmp_path):
    forge, f = await opened(tmp_path, "x = 1 if y is None else y\n")
    llm = Judge(
        RecheckResult(
            change_summary="The None case is now handled before the division.",
            reasoning="The guard covers the input the claim described.",
            verdict="fixed",
            evidence=[Evidence(file="f0.py", line_start=1, line_end=1, note="guard")],
        )
    )
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(
        threads,
        root=tmp_path,
        touched={"f0.py"},
        llm=llm,
        redactor=Redactor(),
        review_id="review",
    )
    posted = await publish(forge, None, 7, 2, 1, verdicts, HEAD)
    assert llm.calls == 1 and verdicts[0].judged
    assert forge.discussions[0].resolved
    assert "The None case is now handled" in posted[0]["body"]


async def test_still_open_when_the_model_says_so(tmp_path):
    forge, f = await opened(tmp_path, "x = 1 if y else 0\n")
    llm = Judge(
        RecheckResult(
            change_summary="The line was reformatted.",
            reasoning="The behaviour the claim describes is unchanged.",
            verdict="not_fixed",
        )
    )
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(
        threads, root=tmp_path, touched={"f0.py"}, llm=llm, redactor=Redactor()
    )
    await publish(forge, None, 7, 2, 1, verdicts, HEAD)
    assert verdicts[0].verdict == "not_fixed"
    assert not forge.discussions[0].resolved


async def test_deleted_file_is_obsolete_and_no_judgement_is_a_fix(tmp_path):
    forge, f = await opened(tmp_path)
    (tmp_path / "f0.py").unlink()
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(threads, root=tmp_path, touched=None)
    assert verdicts[0].verdict == "obsolete"

    # Without a gateway, an ambiguous thread stays open rather than closing.
    forge, f = await opened(tmp_path, "x = 2\n")
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(threads, root=tmp_path, touched={"f0.py"}, llm=None)
    assert verdicts[0].verdict == "unverifiable"

    # The judgement cap has the same effect once it is reached.
    llm = Judge(RecheckResult(verdict="fixed"))
    verdicts = await evaluate(
        threads, root=tmp_path, touched={"f0.py"}, llm=llm, limit=0
    )
    assert verdicts[0].verdict == "unverifiable" and llm.calls == 0


async def test_draft_renders_without_touching_the_merge_request(tmp_path):
    forge, f = await opened(tmp_path)
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(threads, root=tmp_path, touched={"other.py"})
    posted = await publish(forge, None, 7, 2, 1, verdicts, HEAD, draft=True)
    assert posted[0]["body"] and not posted[0]["resolved"]
    assert not forge.replies and not forge.discussions[0].resolved


async def test_recheck_command_queues_without_superseding_a_review(store, tmp_path):
    forge, _ = await opened(tmp_path)
    forge.roles[10] = 30
    queue = FakeQueue()
    ctx = {"forge": forge, "store": store, "redis": queue}
    assert await command(ctx, 7, 2, 10, "/ai recheck", note_id=5) == "queued"
    assert queue.jobs == [("recheck_review", (7, 2))]
    forge.roles[11] = 10
    assert await command(ctx, 7, 2, 11, "/ai recheck", note_id=6) == "forbidden"


async def test_a_judgement_can_only_be_more_conservative(tmp_path):
    forge, f = await opened(tmp_path, "x = 2\n")
    llm = Judge(
        RecheckResult(
            change_summary="The value changed.",
            reasoning="The claim survives the change.",
            verdict="not_fixed",
        )
    )
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(
        threads,
        root=tmp_path,
        touched={"f0.py"},
        absent={f.fingerprint},
        llm=llm,
        redactor=Redactor(),
    )
    await publish(forge, None, 7, 2, 1, verdicts, HEAD)
    # The diff implied a fix; the judge withdrew it, so the thread stays open.
    assert llm.calls == 1 and verdicts[0].verdict == "not_fixed"
    assert not forge.discussions[0].resolved

    # A judgement that fails leaves the diff's own answer standing.
    class Broken:
        async def complete(self, **kwargs):
            raise RuntimeError("gateway down")

    verdicts = await evaluate(
        threads,
        root=tmp_path,
        touched={"f0.py"},
        absent={f.fingerprint},
        llm=Broken(),
        redactor=Redactor(),
    )
    assert verdicts[0].verdict == "fixed" and not verdicts[0].judged


async def test_a_claim_this_run_still_reports_is_never_judged_fixed(tmp_path):
    forge, f = await opened(tmp_path, "x = 2\n")
    llm = Judge(RecheckResult(verdict="fixed"))
    threads = await collect(forge, None, 7, 2, [f], HEAD)
    verdicts = await evaluate(
        threads,
        root=tmp_path,
        touched={"f0.py"},
        reported={f.fingerprint},
        llm=llm,
        redactor=Redactor(),
    )
    assert verdicts[0].verdict == "not_fixed" and llm.calls == 0
