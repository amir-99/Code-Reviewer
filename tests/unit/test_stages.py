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
