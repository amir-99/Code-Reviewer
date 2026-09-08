import asyncio
import time
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
from reviewer.orchestrator.stages import execute, fan_out
from reviewer.services.forge.gitlab import MergeRequestContext


def bundle(tmp_path, count=40):
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
                        DiffLine(text="x=1", new_line=1, old_line=None, kind="added")
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


async def test_forty_files_covered_and_fanout_parallel(tmp_path):
    b = bundle(tmp_path)
    config = ProjectConfig()
    assert len(partition(b)) == 40
    start = time.monotonic()
    results = await fan_out(b, Echo(), config)
    elapsed = time.monotonic() - start
    assert all(len(r.examined) == 40 and not r.partial for r in results)
    assert elapsed < 1.2  # sequential baseline is at least 4*40*.01=1.6s


async def test_missing_coverage_reruns_exactly_once(tmp_path):
    class Missing:
        async def complete(self, **kwargs):
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=[], units_skipped=[], skip_reason=None
                ),
            )

    result = await execute("purpose", bundle(tmp_path, 1), Missing(), ProjectConfig())
    assert result.attempts == 2 and result.partial and len(result.skipped) == 1


async def test_budget_exhaustion_is_partial(tmp_path):
    from reviewer.orchestrator.budget import BudgetExhausted

    class Exhausted:
        async def complete(self, **kwargs):
            raise BudgetExhausted()

    result = await execute("purpose", bundle(tmp_path, 1), Exhausted(), ProjectConfig())
    assert result.partial and result.skipped


async def test_units_overlap_with_limit_and_return_in_dispatch_order(
    tmp_path, monkeypatch
):
    from reviewer.agents.base import TemplateAgent

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

    monkeypatch.setattr(TemplateAgent, "run", run)
    b = bundle(tmp_path, 5)
    task = asyncio.create_task(
        execute("tests_", b, None, ProjectConfig(unit_concurrency=2))
    )
    await asyncio.wait_for(two_started.wait(), 1)
    assert len(started) == 2
    release.set()
    result = await asyncio.wait_for(task, 1)
    assert peak == 2
    assert result.examined == [u.id for u in partition(b, "file_group")]
    assert not result.partial


async def test_cancelling_stage_drains_active_units(tmp_path, monkeypatch):
    import pytest

    from reviewer.agents.base import TemplateAgent

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

    monkeypatch.setattr(TemplateAgent, "run", run)
    task = asyncio.create_task(
        execute("tests_", bundle(tmp_path, 5), None, ProjectConfig())
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


async def test_repeated_context_is_present_once_without_losing_distinct_files(tmp_path):
    from reviewer.agents.base import TemplateAgent
    from reviewer.findings.models import ContextRequest

    prompts = []

    class Requests:
        async def complete(self, **kwargs):
            prompts.append(kwargs["user"])
            return StageEnvelope(
                findings=[],
                coverage=Coverage(
                    units_examined=["change:unit-0"], units_skipped=[], skip_reason=None
                ),
                context_requests=[
                    ContextRequest(kind="file", target="f0.py", reason="check")
                ],
            )

    responses = iter(
        [
            {"f0.py": "unique_context_a"},
            {"f0.py": "unique_context_a", "f1.py": "unique_context_b"},
        ]
    )

    async def provider(requests):
        return next(responses)

    b = bundle(tmp_path, 1)
    await TemplateAgent("purpose").run(
        b, partition(b, "whole_change")[0], Requests(), provider
    )
    assert len(prompts) == 3
    assert prompts[-1].count("unique_context_a") == 1
    assert prompts[-1].count("unique_context_b") == 1


async def test_concurrent_retry_and_failure_preserve_other_units(tmp_path, monkeypatch):
    from collections import Counter

    from reviewer.agents.base import TemplateAgent

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

    monkeypatch.setattr(TemplateAgent, "run", run)
    result = await execute("tests_", bundle(tmp_path, 3), None, ProjectConfig())
    assert result.examined == ["f0.py:unit-0", "f2.py:unit-0"]
    assert result.skipped == ["f1.py:unit-0"]
    assert result.failed and result.partial
    assert attempts == {"f0.py:unit-0": 2, "f1.py:unit-0": 1, "f2.py:unit-0": 1}
