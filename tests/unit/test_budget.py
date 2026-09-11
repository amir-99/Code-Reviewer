import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from reviewer.context.models import Budget
from reviewer.orchestrator.budget import BudgetExhausted, BudgetTracker


def tracker(seconds=60, protected=0):
    return BudgetTracker(
        Budget(
            token_ceiling=100,
            deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
            model_tier={},
        ),
        protected,
    )


async def test_contention_waits_for_refund():
    budget = tracker()
    await budget.reserve(80)
    waiting = asyncio.create_task(budget.reserve(60))
    await asyncio.sleep(0)
    assert not waiting.done()
    await budget.settle(80, 20)
    await asyncio.wait_for(waiting, 1)
    assert budget.reserved == 60 and budget.budget.tokens_used == 20
    await budget.settle(60, 10)
    assert budget.reserved == 0


async def test_real_exhaustion_after_settlement():
    budget = tracker()
    await budget.reserve(80)
    waiting = asyncio.create_task(budget.reserve(60))
    await asyncio.sleep(0)
    await budget.settle(80, 70)
    with pytest.raises(BudgetExhausted):
        await asyncio.wait_for(waiting, 1)
    assert budget.reserved == 0


async def test_waiter_deadline_and_cancellation_do_not_leak_reservations():
    budget = tracker(seconds=0.03)
    await budget.reserve(80)
    with pytest.raises(BudgetExhausted):
        await budget.reserve(60)
    assert budget.reserved == 80
    budget = tracker()
    await budget.reserve(80)
    waiting = asyncio.create_task(budget.reserve(60))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await budget.settle(80, 0)
    assert budget.reserved == 0
    await budget.reserve(100)


async def test_final_stages_can_use_protected_allowance():
    budget = tracker(protected=40)
    with pytest.raises(BudgetExhausted):
        await budget.reserve(70, stage="correctness")
    await budget.reserve(60, stage="correctness")
    await budget.settle(60, 60)
    for stage in ("system_context", "verification"):
        await budget.reserve(20, stage=stage)
        await budget.settle(20, 20)
    assert budget.budget.tokens_used == 100


def test_repository_cannot_override_scheduling_controls(tmp_path):
    from reviewer.config.loader import load_project

    for setting in (
        "unit_concurrency: 16",
        "verification_concurrency: 16",
        "final_stage_token_reserve: 0",
        "gateway_concurrency: 64",
    ):
        with pytest.raises(ValueError, match="operator-only"):
            load_project(tmp_path / "missing.json", 7, setting)
