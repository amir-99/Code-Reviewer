import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from reviewer.config.schema import ProjectConfig
from reviewer.context.models import (
    Budget,
    ChangedFile,
    CodeContext,
    ContextBundle,
    DiffLine,
    Linkage,
)
from reviewer.context.partition import partition
from reviewer.findings.models import Coverage, StageEnvelope
from reviewer.orchestrator.stages import execute
from reviewer.services.forge.gitlab import MergeRequestContext

# A line long enough that no two files share a 256-byte unit.
LONG = "x = " + "1" * 150
# One file per unit, so unit-level concurrency and retries are observable.
SEPARATE = {"review": {"unit_tokens": 256}}


def bundle(tmp_path, count=40, text="x=1"):
    return ContextBundle(
        review_id=uuid4(),
        mr=MergeRequestContext(project_id=1, iid=1, head_sha="a" * 40),
        linkage=Linkage(),
        code=CodeContext(
            merge_base_sha="b" * 40,
            head_sha="a" * 40,
            target_branch="main",
            worktree_path=tmp_path,
            total_changed_lines=count,
            files=[
                ChangedFile(
                    path=f"f{n}.py",
                    change_type="added",
                    lines=[
                        DiffLine(text=text, new_line=1, old_line=None, kind="added")
                    ],
                )
                for n in range(count)
            ],
        ),
        budget=Budget(
            token_ceiling=100000,
            deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            model_tier={},
        ),
    )


class Echo:
    async def complete(self, **kwargs):
        import re
        from html import unescape

        user = unescape(kwargs["user"])
        ids = re.findall(r'"id": "([^"]+:unit-\d+)"', user)
        await asyncio.sleep(0.01)
        return StageEnvelope(
            findings=[],
            coverage=Coverage(units_examined=ids, units_skipped=[], skip_reason=None),
        )


async def test_small_files_pack_and_every_unit_is_covered(tmp_path):
    b = bundle(tmp_path)
    assert len(partition(b)) == 1
    result = await execute("defect_review", b, Echo(), ProjectConfig())
    assert result.examined == [u.id for u in partition(b)] and not result.partial
    separate = bundle(tmp_path, 40, LONG)
    config = ProjectConfig(**SEPARATE)
    assert len(partition(separate, 256)) == 40
    result = await execute("defect_review", separate, Echo(), config)
    assert len(result.examined) == 40 and not result.partial


async def test_missing_coverage_reruns_exactly_once(tmp_path):
    class Missing:
        async def complete(self, **kwargs):
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=[], units_skipped=[], skip_reason=None
                ),
            )

    result = await execute(
        "defect_review", bundle(tmp_path, 1), Missing(), ProjectConfig()
    )
    assert result.attempts == 2 and result.partial and len(result.skipped) == 1


async def test_budget_exhaustion_is_partial(tmp_path):
    from reviewer.orchestrator.budget import BudgetExhausted

    class Exhausted:
        async def complete(self, **kwargs):
            raise BudgetExhausted()

    result = await execute(
        "defect_review", bundle(tmp_path, 1), Exhausted(), ProjectConfig()
    )
    assert result.partial and result.skipped


async def test_units_overlap_with_limit_and_return_in_dispatch_order(
    tmp_path, monkeypatch
):
    from reviewer.agents.base import DefectReviewAgent

    started = []
    active = 0
    peak = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def run(self, bundle, unit, *args):
        nonlocal active, peak
        started.append(unit.id)
        active += 1
        peak = max(peak, active)
        try:
            if len(started) == 2:
                two_started.set()
            await release.wait()
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=[unit.id], units_skipped=[], skip_reason=None
                ),
            )
        finally:
            active -= 1

    monkeypatch.setattr(DefectReviewAgent, "run", run)
    b = bundle(tmp_path, 5, LONG)
    task = asyncio.create_task(
        execute("defect_review", b, None, ProjectConfig(unit_concurrency=2, **SEPARATE))
    )
    await asyncio.wait_for(two_started.wait(), 1)
    assert len(started) == 2
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert peak == 2
    assert result.examined == [u.id for u in partition(b, 256)]
    assert not result.partial


async def test_cancelling_stage_drains_active_units(tmp_path, monkeypatch):
    import pytest

    from reviewer.agents.base import DefectReviewAgent

    active = 0
    started = asyncio.Event()

    async def run(*args):
        nonlocal active
        active += 1
        try:
            if active == 2:
                started.set()
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(DefectReviewAgent, "run", run)
    task = asyncio.create_task(
        execute(
            "defect_review",
            bundle(tmp_path, 5, LONG),
            None,
            ProjectConfig(**SEPARATE),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


async def test_requested_context_is_supplied_once_in_the_single_round(tmp_path):
    from reviewer.agents.base import DefectReviewAgent
    from reviewer.findings.models import ContextRequest

    prompts = []

    class Requests:
        async def complete(self, **kwargs):
            prompts.append(kwargs["user"])
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=["f0.py:unit-0"], units_skipped=[], skip_reason=None
                ),
                context_requests=[
                    ContextRequest(kind="file", target="f0.py", reason="check")
                ],
            )

    async def provider(requests):
        return {"f0.py": "unique_context_a", "f1.py": "unique_context_b"}

    b = bundle(tmp_path, 1)
    await DefectReviewAgent().run(b, partition(b)[0], Requests(), provider)
    # One context round: the second answer is final even if it asks again.
    assert len(prompts) == 2
    assert prompts[-1].count("unique_context_a") == 1
    assert prompts[-1].count("unique_context_b") == 1


async def test_concurrent_retry_and_failure_preserve_other_units(tmp_path, monkeypatch):
    from collections import Counter

    from reviewer.agents.base import DefectReviewAgent

    attempts = Counter()

    async def run(self, bundle, unit, *args):
        attempts[unit.id] += 1
        await asyncio.sleep(0)
        if unit.id == "f1.py:unit-0":
            raise RuntimeError("unavailable")
        missing = unit.id == "f0.py:unit-0" and attempts[unit.id] == 1
        return StageEnvelope(
            findings=[],
            coverage=Coverage(
                units_examined=[] if missing else [unit.id],
                units_skipped=[],
                skip_reason=None,
            ),
        )

    monkeypatch.setattr(DefectReviewAgent, "run", run)
    result = await execute(
        "defect_review", bundle(tmp_path, 3, LONG), None, ProjectConfig(**SEPARATE)
    )
    assert result.examined == ["f0.py:unit-0", "f2.py:unit-0"]
    assert result.skipped == ["f1.py:unit-0"]
    assert result.failed and result.partial
    assert attempts == {"f0.py:unit-0": 2, "f1.py:unit-0": 1, "f2.py:unit-0": 1}


async def test_unit_progress_tracks_logical_units_and_reuse(tmp_path, monkeypatch):
    events = []

    async def record(kind, data):
        if kind == "unit_progress":
            events.append(data)

    monkeypatch.setattr("reviewer.orchestrator.stages.record", record)
    b = bundle(tmp_path, 4, LONG)
    config = ProjectConfig(unit_concurrency=2, **SEPARATE)
    result = await execute("defect_review", b, Echo(), config)
    assert events[0]["total"] == events[0]["idle"] == 4
    assert events[-1]["completed"] == 4
    assert events[-1]["running"] == events[-1]["idle"] == 0
    assert max(e["running"] for e in events) == 2
    assert all(
        e["total"] == e["completed"] + e["running"] + e["idle"] + e["stopped"]
        for e in events
    )
    events.clear()
    await execute("defect_review", b, Echo(), config, previous=result)
    assert len(events) == 1
    assert events[0]["completed"] == 4 and events[0]["running"] == 0


async def test_progress_coverage_retry_does_not_inflate_total(tmp_path, monkeypatch):
    events = []

    async def record(kind, data):
        if kind == "unit_progress":
            events.append(data)

    class Missing:
        async def complete(self, **kwargs):
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=[], units_skipped=[], skip_reason=None
                ),
            )

    monkeypatch.setattr("reviewer.orchestrator.stages.record", record)
    result = await execute(
        "defect_review", bundle(tmp_path, 1), Missing(), ProjectConfig()
    )
    assert result.attempts == 2
    assert events[-1]["total"] == 1
    assert events[-1]["completed"] == events[-1]["running"] == 0
    assert events[-1]["stopped"] == 1
