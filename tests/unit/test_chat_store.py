"""Chat storage: its own budget, its own rows, never the review's."""

from datetime import timedelta

from reviewer.config.schema import ROLES, ChatConfig, ProjectConfig
from reviewer.store.models import Account, LLMCall, utcnow


async def call(store, review_id, stage, tokens_in, tokens_out, cost=0.0):
    async with store.transaction() as session:
        session.add(
            LLMCall(
                review_id=review_id,
                stage=stage,
                model="vendor/model",
                prompt_version="1.0.0",
                prompt_hash="h",
                prompt_blob_ref="p",
                response_blob_ref="r",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=1,
                cost=cost,
                outcome="success",
            )
        )


async def test_chat_spend_is_reported_apart_from_the_review(store):
    review = await store.accept(7, 2, "a" * 40, "evt-1")
    await call(store, review.id, "defect_review", 1000, 100, 0.01)
    await call(store, review.id, "chat", 500, 50, 0.02)
    await call(store, review.id, "chat", 500, 50, 0.02)
    spend = await store.spend(review.id)
    assert spend["tokens"] == 1100 and spend["calls"] == 1
    assert spend["cost"] == 0.01
    assert [r["role"] for r in spend["roles"]] == ["defect_review"]
    assert spend["chat"]["calls"] == 2 and spend["chat"]["tokens"] == 1100
    assert spend["chat"]["cost"] == 0.04
    assert await store.chat_tokens(review.id) == 1100


async def test_chat_role_is_configurable_and_budget_is_separate():
    assert "chat" in ROLES
    config = ProjectConfig.model_validate(
        {"chat": {"token_ceiling": 5000, "timeout_s": 30}}
    )
    assert config.chat == ChatConfig(token_ceiling=5000, timeout_s=30)
    assert config.review.token_ceiling == 120000


async def test_chat_messages_sequence_and_settle_once(store):
    review = await store.accept(7, 2, "a" * 40, "evt-2")
    async with store.transaction() as session:
        account = Account(login="asker", display_name="Asker", role="user")
        session.add(account)
        await session.flush()
        user_id = account.id
    first = await store.add_chat_message(
        review.id, user_id, "why blocker? password=supersecretvalue123", {"gateway": 1}
    )
    second = await store.add_chat_message(review.id, user_id, "and this?", None)
    assert (first["sequence"], second["sequence"]) == (1, 2)
    assert "supersecretvalue123" not in first["question"]
    assert "credential_refs" not in first
    assert await store.chat_pending(review.id)
    assert await store.chat_recent(user_id, utcnow() - timedelta(hours=1)) == 2

    assert await store.finish_chat_message(
        first["id"],
        status="answered",
        answer="Because the verifier confirmed it.",
        citations=[{"kind": "finding", "ref": "f1"}],
        context_used=["src/a.py"],
        model="vendor/model",
        tokens_in=10,
        tokens_out=5,
    )
    # arq may deliver the job again; the first settlement is the only one.
    assert not await store.finish_chat_message(
        first["id"], status="failed", error="later"
    )
    rows = await store.chat_messages(review.id)
    assert rows[0]["status"] == "answered" and rows[0]["tokens"] == 15
    assert rows[0]["citations"] == [{"kind": "finding", "ref": "f1"}]
    assert rows[1]["status"] == "pending"
    assert await store.stale_chat_messages(utcnow() + timedelta(seconds=1)) == [
        second["id"]
    ]
