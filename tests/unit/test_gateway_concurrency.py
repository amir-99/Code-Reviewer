import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from reviewer.config.schema import Settings
from reviewer.context.models import Budget
from reviewer.context.redaction import Redactor
from reviewer.findings.models import StageEnvelope
from reviewer.orchestrator.budget import BudgetExhausted, BudgetTracker
from reviewer.services.llm.client import GatewayClient


def client(handler, semaphore, seconds=60):
    audits = []

    async def write(**fields):
        audits.append(fields)

    llm = GatewayClient(
        Settings(_env_file=None, gateway_base_url="https://gateway.internal/v1"),
        SimpleNamespace(write=write),
        BudgetTracker(
            Budget(
                token_ceiling=1000000,
                deadline_at=datetime.now(UTC) + timedelta(seconds=seconds),
                model_tier={},
            )
        ),
        Redactor(),
        httpx.MockTransport(handler),
        semaphore=semaphore,
    )
    return llm, audits


async def complete(llm):
    return await llm.complete(
        stage="purpose",
        tier="purpose",
        system="review",
        user="code",
        response_model=StageEnvelope,
        review_id="review",
        max_tokens=100,
        timeout_s=2,
    )


async def test_shared_limit_bounds_calls_across_clients_and_audits_every_attempt():
    active = 0
    peak = 0
    full = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        if active == 2:
            full.set()
        try:
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"findings": [], "coverage": {"units_examined": [], "units_skipped": [], "skip_reason": null}}'
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )
        finally:
            active -= 1

    slots = asyncio.Semaphore(2)
    first, audits1 = client(handler, slots)
    second, audits2 = client(handler, slots)
    tasks = [asyncio.create_task(complete([first, second][n % 2])) for n in range(6)]
    try:
        await asyncio.wait_for(full.wait(), 1)
        assert active == peak == 2
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert peak == 2 and len(audits1) + len(audits2) == 6
        assert first.budget.reserved == second.budget.reserved == 0
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await first.close()
        await second.close()


async def test_queue_deadline_starts_no_attempt_and_reserves_no_tokens():
    def handler(request):
        pytest.fail("queued call must not reach transport")

    slots = asyncio.Semaphore(1)
    await slots.acquire()
    llm, audits = client(handler, slots, seconds=0.02)
    try:
        with pytest.raises(BudgetExhausted):
            await asyncio.wait_for(complete(llm), 1)
        assert audits == [] and llm.budget.reserved == 0
        assert slots.locked()
    finally:
        slots.release()
        await llm.close()


async def test_cancelled_call_releases_capacity_and_token_reservation():
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.Event().wait()

    slots = asyncio.Semaphore(1)
    llm, audits = client(handler, slots)
    task = asyncio.create_task(complete(llm))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert llm.budget.reserved == 0 and not slots.locked()
        assert len(audits) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await llm.close()


async def test_cancellation_during_queue_telemetry_releases_acquired_slot(monkeypatch):
    entered = asyncio.Event()

    async def record(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("reviewer.services.llm.client.record", record)
    slots = asyncio.Semaphore(1)
    llm, audits = client(lambda request: None, slots)
    task = asyncio.create_task(complete(llm))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not slots.locked() and audits == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await llm.close()
