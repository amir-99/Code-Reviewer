from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from reviewer.config.schema import Settings
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
