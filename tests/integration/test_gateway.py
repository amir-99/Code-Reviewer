from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from reviewer.config.models import resolve
from reviewer.config.schema import ModelProfile, ModelSpec, ProjectConfig, Settings
from reviewer.context.models import Budget
from reviewer.context.redaction import Redactor
from reviewer.findings.models import StageEnvelope
from reviewer.orchestrator.budget import BudgetTracker
from reviewer.services.llm.client import GatewayClient, StageFailed
from reviewer.store.audit import Audit, BlobStore
from reviewer.store.models import LLMCall


async def test_malformed_output_reparse_audited_and_stage_fails(store, tmp_path):
    def handler(req):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "malformed"}}]}
        )

    settings = Settings(
        gateway_base_url="https://gateway.internal/v1", model_strong="approved"
    )
    budget = BudgetTracker(
        Budget(
            token_ceiling=100000,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            model_tier={},
        )
    )
    llm = GatewayClient(
        settings,
        Audit(store, BlobStore(tmp_path / "blobs")),
        budget,
        Redactor(),
        httpx.MockTransport(handler),
    )
    with pytest.raises(StageFailed):
        await llm.complete(
            stage="purpose",
            tier="strong",
            system="review",
            user="code",
            response_model=StageEnvelope,
            review_id="a",
            max_tokens=100,
            timeout_s=2,
        )
    async with store.sessions() as s:
        calls = (await s.scalars(select(LLMCall))).all()
        assert len(calls) == 2 and all(x.prompt_version == "1.0.0" for x in calls)
    await llm.close()


async def test_transport_retries_exactly_twice_and_audits(store, tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        raise httpx.ConnectError("unavailable")

    settings = Settings(
        gateway_base_url="https://gateway.internal/v1", model_strong="approved"
    )
    budget = BudgetTracker(
        Budget(
            token_ceiling=100000,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            model_tier={},
        )
    )
    llm = GatewayClient(
        settings,
        Audit(store, BlobStore(tmp_path / "blobs")),
        budget,
        Redactor(),
        httpx.MockTransport(handler),
    )
    with pytest.raises(StageFailed):
        await llm.complete(
            stage="purpose",
            tier="strong",
            system="review",
            user="code",
            response_model=StageEnvelope,
            review_id="b",
            max_tokens=100,
            timeout_s=2,
        )
    assert len(calls) == 3
    async with store.sessions() as session:
        assert len((await session.scalars(select(LLMCall))).all()) == 3
    await llm.close()


@pytest.mark.parametrize(
    ("content", "finish_reason", "expected"),
    [
        ("malformed-private-content", "length", "invalid_json"),
        ("{}", "private-upstream-value", "schema_validation"),
    ],
)
async def test_attempt_diagnostics_are_persisted_without_response_content(
    store, tmp_path, content, finish_reason, expected
):
    from reviewer.telemetry.activity import sink

    review = await store.accept(7, 2, "a" * 40, "diagnostics")

    def handler(req):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": content}, "finish_reason": finish_reason}
                ]
            },
        )

    budget = BudgetTracker(
        Budget(
            token_ceiling=100000,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            model_tier={},
        )
    )
    llm = GatewayClient(
        Settings(
            gateway_base_url="https://gateway.internal/v1", model_strong="approved"
        ),
        Audit(store, BlobStore(tmp_path / "blobs")),
        budget,
        Redactor(),
        httpx.MockTransport(handler),
    )
    token = sink.set((store, review.id))
    try:
        with pytest.raises(StageFailed):
            await llm.complete(
                stage="design",
                tier="strong",
                system="review",
                user="private-code",
                response_model=StageEnvelope,
                review_id=review.id,
                max_tokens=100,
                timeout_s=2,
            )
    finally:
        sink.reset(token)
        await llm.close()
    diagnostics = [
        e["data"] for e in await store.events(review.id) if e["kind"] == "llm_attempt"
    ]
    assert len(diagnostics) == 2
    assert [e["parse_attempt"] for e in diagnostics] == [1, 2]
    assert all(e["validation_failure"] == expected for e in diagnostics)
    assert all(
        e["finish_reason"] == ("length" if finish_reason == "length" else "unknown")
        for e in diagnostics
    )
    assert "private" not in str(diagnostics)
    async with store.sessions() as session:
        assert len((await session.scalars(select(LLMCall))).all()) == 2


async def test_concurrent_gateway_calls_wait_for_budget_and_keep_audits(
    store, tmp_path
):
    import asyncio

    first_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release.wait()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"findings": [], "coverage": {"units_examined": [], "units_skipped": [], "skip_reason": null}}'
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    budget = BudgetTracker(
        Budget(
            token_ceiling=250,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1),
            model_tier={},
        )
    )
    llm = GatewayClient(
        Settings(
            gateway_base_url="https://gateway.internal/v1", model_strong="approved"
        ),
        Audit(store, BlobStore(tmp_path / "blobs")),
        budget,
        Redactor(),
        httpx.MockTransport(handler),
    )

    async def complete():
        return await llm.complete(
            stage="correctness",
            tier="strong",
            system="review",
            user="code",
            response_model=StageEnvelope,
            review_id="concurrent",
            max_tokens=100,
            timeout_s=2,
        )

    tasks = [asyncio.create_task(complete())]
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        tasks.append(asyncio.create_task(complete()))
        await asyncio.sleep(0)
        assert calls == 1 and not tasks[-1].done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        assert calls == 2 and budget.reserved == 0 and budget.budget.tokens_used == 40
        async with store.sessions() as session:
            assert len((await session.scalars(select(LLMCall))).all()) == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await llm.close()


async def test_each_role_runs_on_its_own_model_within_its_own_limits(store, tmp_path):
    """One client, three roles, three models — and each model's own ceilings."""
    from reviewer.telemetry.activity import sink

    sent = []

    def handler(req):
        import json as jsonlib

        sent.append(jsonlib.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"findings": [], "coverage": {"units_examined": [], "units_skipped": [], "skip_reason": null}}'
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    settings = Settings(
        gateway_base_url="https://gateway.internal/v1",
        model_roles={
            "correctness": "vendor/strong",
            "line_review": "vendor/cheap",
            "verification": "vendor/judge",
        },
        model_limits={
            "vendor/cheap": {"max_output_tokens": 4096, "context_tokens": 9000}
        },
        model_context_tokens=200000,
    )
    review = await store.accept(7, 2, "b" * 40, "roles")
    llm = GatewayClient(
        settings,
        Audit(store, BlobStore(tmp_path / "blobs")),
        BudgetTracker(
            Budget(
                token_ceiling=1000000,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
                model_tier={},
            )
        ),
        Redactor(),
        httpx.MockTransport(handler),
        models=resolve(settings),
    )
    token = sink.set((store, review.id))
    try:
        for role in ("correctness", "line_review", "verification"):
            await llm.complete(
                stage=role,
                tier=role,
                system="review",
                user="code",
                response_model=StageEnvelope,
                review_id=review.id,
                max_tokens=16000,
                timeout_s=2,
            )
    finally:
        sink.reset(token)
        await llm.close()
    assert [body["model"] for body in sent] == [
        "vendor/strong",
        "vendor/cheap",
        "vendor/judge",
    ]
    # A stage may not ask for more output than its own model will return, and a
    # model that declares no ceiling is left alone.
    assert [body["max_tokens"] for body in sent] == [16000, 4096, 16000]
    # The attempt diagnostics name the model that served each call, so an
    # operator reading the activity feed can see what produced the review.
    attempts = [
        e["data"] for e in await store.events(review.id) if e["kind"] == "llm_attempt"
    ]
    assert [(a["role"], a["model"]) for a in attempts] == [
        ("correctness", "vendor/strong"),
        ("line_review", "vendor/cheap"),
        ("verification", "vendor/judge"),
    ]


async def test_a_role_sends_its_configured_effort_and_its_own_context_window(
    store, tmp_path
):
    sent = []

    def handler(req):
        import json as jsonlib

        sent.append(jsonlib.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"findings": [], "coverage": {"units_examined": [], "units_skipped": [], "skip_reason": null}}'
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    settings = Settings(gateway_base_url="https://gateway.internal/v1")
    config = ProjectConfig(
        models=ModelProfile(
            roles={
                "design": ModelSpec(model="vendor/thinker", reasoning_effort="high"),
                "complexity": ModelSpec(model="vendor/tiny", context_tokens=1200),
            }
        )
    )
    llm = GatewayClient(
        settings,
        Audit(store, BlobStore(tmp_path / "blobs")),
        BudgetTracker(
            Budget(
                token_ceiling=1000000,
                deadline_at=datetime.now(UTC) + timedelta(minutes=1),
                model_tier={},
            )
        ),
        Redactor(),
        httpx.MockTransport(handler),
        models=resolve(settings, config),
    )
    await llm.complete(
        stage="design",
        tier="design",
        system="review",
        user="code",
        response_model=StageEnvelope,
        review_id="effort",
        max_tokens=100,
        timeout_s=2,
    )
    assert sent[0]["reasoning_effort"] == "high"
    # A prompt that fits the installation's window but not this role's model is
    # refused here rather than by the gateway, one stage at a time.
    with pytest.raises(StageFailed):
        await llm.complete(
            stage="complexity",
            tier="complexity",
            system="review",
            user="x" * 4000,
            response_model=StageEnvelope,
            review_id="effort",
            max_tokens=100,
            timeout_s=2,
        )
    # A role nothing selected a model for is still refused explicitly.
    llm.specs.pop("purpose")
    with pytest.raises(StageFailed):
        await llm.complete(
            stage="purpose",
            tier="purpose",
            system="review",
            user="code",
            response_model=StageEnvelope,
            review_id="effort",
            max_tokens=100,
            timeout_s=2,
        )
    await llm.close()
