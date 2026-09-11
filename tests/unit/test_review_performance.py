import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from test_stages import Echo, bundle

from reviewer.agents.base import TemplateAgent
from reviewer.config.schema import ProjectConfig
from reviewer.context.redaction import Redactor
from reviewer.findings.models import Coverage, StageEnvelope
from reviewer.orchestrator.deadlines import cutoff
from reviewer.orchestrator.stages import execute


def envelope(unit):
    return StageEnvelope(
        findings=[],
        coverage=Coverage(units_examined=[unit.id], units_skipped=[], skip_reason=None),
    )


async def test_restart_keeps_completed_units_even_when_deadline_expired(
    store, tmp_path, monkeypatch
):
    review = await store.accept(7, 2, "a" * 40, "checkpoint")
    b = bundle(tmp_path, 3)
    b.review_id = UUID(review.id)
    config = ProjectConfig(unit_concurrency=1)
    second = asyncio.Event()
    calls = []

    async def run(self, bundle, unit, *args):
        calls.append(unit.id)
        if unit.id == "f1.py:unit-0":
            second.set()
            await asyncio.Event().wait()
        return envelope(unit)

    monkeypatch.setattr(TemplateAgent, "run", run)
    task = asyncio.create_task(
        execute("tests_", b, None, config, store=store, redactor=Redactor())
    )
    try:
        await asyncio.wait_for(second.wait(), 2)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await store.unit_results(review.id, "tests_")) == 1
    b.budget.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    result = await execute("tests_", b, None, config, store=store, redactor=Redactor())
    assert calls == ["f0.py:unit-0", "f1.py:unit-0"]
    assert result.examined == ["f0.py:unit-0"]
    assert result.skipped == ["f1.py:unit-0", "f2.py:unit-0"] and result.partial


async def test_resume_only_calls_unfinished_units_and_invalidates_changed_inputs(
    store, tmp_path, monkeypatch
):
    review = await store.accept(7, 2, "a" * 40, "identities")
    b = bundle(tmp_path, 2)
    b.review_id = UUID(review.id)
    config = ProjectConfig(unit_concurrency=1)
    calls = []

    async def run(self, bundle, unit, *args):
        calls.append(unit.id)
        return envelope(unit)

    monkeypatch.setattr(TemplateAgent, "run", run)
    llm = type("Models", (), {"models": {"tests_": "approved-a"}})()
    result = await execute("tests_", b, llm, config, store=store)
    await execute("tests_", b, llm, config, store=store)
    assert len(calls) == 2
    b.code.files[1].lines[0].text = "changed = 2"
    await execute("tests_", b, llm, config, store=store, previous=result)
    assert calls == ["f0.py:unit-0", "f1.py:unit-0", "f1.py:unit-0"]
    llm.models["tests_"] = "approved-b"
    await execute("tests_", b, llm, config, store=store)
    assert len(calls) == 5


async def test_checkpoint_redacts_model_findings(store, tmp_path, monkeypatch):
    from test_findings import finding

    from reviewer.findings.models import ProposedFinding

    review = await store.accept(7, 2, "a" * 40, "redact-unit")
    b = bundle(tmp_path, 1)
    b.review_id = UUID(review.id)

    async def run(self, bundle, unit, *args):
        result = envelope(unit)
        f = finding()
        f.claim = "private-fixture-value"
        result.findings = [
            ProposedFinding(
                **{key: getattr(f, key) for key in ProposedFinding.model_fields}
            )
        ]
        return result

    monkeypatch.setattr(TemplateAgent, "run", run)
    redactor = Redactor([{"Secret": "private-fixture-value", "RuleID": "fixture"}])
    await execute("tests_", b, None, ProjectConfig(), store=store, redactor=redactor)
    rows = await store.unit_results(review.id, "tests_")
    assert "private-fixture-value" not in json.dumps(rows)
    assert "REDACTED:fixture" in json.dumps(rows)


async def test_unit_timeout_bounds_all_retries_and_preserves_other_units(
    tmp_path, monkeypatch
):
    stopped = []

    async def run(self, bundle, unit, *args):
        if unit.id == "f0.py:unit-0":
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(unit.id)
        return envelope(unit)

    monkeypatch.setattr(TemplateAgent, "run", run)
    result = await asyncio.wait_for(
        execute(
            "tests_", bundle(tmp_path, 2), None, ProjectConfig(unit_timeout_s=0.03)
        ),
        1,
    )
    assert stopped == result.skipped == ["f0.py:unit-0"]
    assert result.examined == ["f1.py:unit-0"] and result.partial


async def test_analysis_cutoff_preserves_final_stages(tmp_path):
    b = bundle(tmp_path, 1)
    config = ProjectConfig()
    b.budget.deadline_at = datetime.now(UTC) + timedelta(seconds=120)
    analysis = await execute("purpose", b, Echo(), config)
    final = await execute("system_context", b, Echo(), config)
    # System Context has a later cutoff than analysis and verification later still.
    assert analysis.partial and not analysis.examined
    assert not final.partial
    assert (
        cutoff(b, config, "purpose")
        < cutoff(b, config, "system_context")
        < cutoff(b, config, "verification")
    )


async def test_triage_selects_bounded_breadth_first_units_and_reports_omissions(
    tmp_path,
):
    b = bundle(tmp_path, 4)
    b.degradations.append("triage_mode")
    b.code.files[0].lines[0].text = "x" * 10000
    config = ProjectConfig(triage_unit_tokens=256, triage_max_units=3)
    result = await execute("tests_", b, Echo(), config)
    assert result.examined == ["f0.py:unit-0", "f1.py:unit-0", "f2.py:unit-0"]
    assert "f0.py:unit-1" in result.skipped and "f3.py:unit-0" in result.skipped
    assert result.partial and result.attempts == 3


async def test_coverage_retry_explicitly_requests_exact_id(tmp_path):
    prompts = []

    class MissingThenCorrect(Echo):
        async def complete(self, **kwargs):
            prompts.append(kwargs["user"])
            result = await super().complete(**kwargs)
            if len(prompts) == 1:
                result.coverage.units_examined = ["unrelated:unit-0"]
            return result

    result = await execute(
        "purpose", bundle(tmp_path, 1), MissingThenCorrect(), ProjectConfig()
    )
    assert result.examined == ["change:unit-0"] and result.attempts == 2
    from html import unescape

    assert '"coverage_retry": true' in unescape(prompts[-1])


async def test_execution_config_is_frozen_on_review(store):
    review = await store.accept(7, 2, "a" * 40, "freeze")
    first = {
        "models": {"tests_": {"model": "approved-first"}},
        "config": {"unit_concurrency": 2},
    }
    assert await store.freeze_execution(review.id, first) == first
    assert (
        await store.freeze_execution(review.id, {"models": {}, "config": {}}) == first
    )
    assert (await store.get(review.id)).execution_config == first


async def test_verification_cutoff_does_not_claim_a_fix_or_call_the_judge(
    tmp_path, monkeypatch
):
    from test_findings import finding

    from reviewer.orchestrator.verification import verify_findings

    b = bundle(tmp_path, 1)
    b.budget.deadline_at = datetime.now(UTC) + timedelta(seconds=10)
    called = []

    async def scan(requests):
        called.append("scan")

    async def verify(*args):
        called.append("judge")

    monkeypatch.setattr("reviewer.orchestrator.verification.verify", verify)
    f = finding()
    await verify_findings([f], b, None, None, scan, 2, config=ProjectConfig())
    assert called == []
    assert f.verification.verdict == "uncertain"
    assert "budget_exhausted" in b.degradations


async def test_checkpoint_failure_is_not_silently_treated_as_completed(tmp_path):
    class Broken:
        async def unit_results(self, *args):
            return {}

        async def save_unit(self, *args):
            raise RuntimeError("storage unavailable")

    with pytest.raises(ExceptionGroup):
        await execute(
            "tests_",
            bundle(tmp_path, 1),
            Echo(),
            ProjectConfig(),
            store=Broken(),
            redactor=Redactor(),
        )


async def test_frozen_configuration_safe_under_concurrent_recovery(store):
    review = await store.accept(7, 2, "a" * 40, "concurrent-freeze")
    configs = [{"models": {"tests_": f"approved-{n}"}} for n in range(2)]
    results = await asyncio.gather(
        *(store.freeze_execution(review.id, config) for config in configs)
    )
    assert results[0] == results[1] == (await store.get(review.id)).execution_config


def test_slow_database_phase_remains_visible_at_warning(monkeypatch):
    from importlib import import_module

    from structlog.testing import capture_logs

    activity_module = import_module("reviewer.telemetry.activity")
    now = [0.0]
    monkeypatch.setattr(activity_module, "monotonic", lambda: now[0])
    with capture_logs() as logs:
        with activity_module.storage_timing("Activity review lock", "review"):
            now[0] = 1.0
    finished = [row for row in logs if row["event"] == "storage_finished"]
    assert finished[0]["log_level"] == "warning"
    assert finished[0]["duration_ms"] == 1000


def test_response_schema_rejects_foreign_coverage_ids_without_inventing_coverage():
    from pydantic import ValidationError

    from reviewer.agents.base import unit_response_model

    model = unit_response_model("change:unit-0")
    response = {
        "findings": [],
        "coverage": {"units_examined": [], "units_skipped": [], "skip_reason": None},
    }
    assert not model.model_validate(response).coverage.units_examined
    response["coverage"]["units_examined"] = ["other-file.py:unit-0"]
    with pytest.raises(ValidationError):
        model.model_validate(response)
    response["coverage"]["units_examined"] = ["change:unit-0"]
    assert model.model_validate(response).coverage.units_examined == ["change:unit-0"]


async def test_cancelled_activity_wait_does_not_strand_sqlite_writer(store):
    review = await store.accept(7, 2, "a" * 40, "writer-cancellation")
    async with store.transaction():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.02):
                await store.append_event(
                    review.id, "tool", {"name": "cancelled writer"}
                )
    await asyncio.wait_for(
        asyncio.gather(
            store.save_unit(
                review.id, "tests_", "a" * 64, {"examined": ["f0.py:unit-0"]}
            ),
            store.append_event(review.id, "tool", {"name": "successful writer"}),
        ),
        2,
    )
    assert len(await store.unit_results(review.id, "tests_")) == 1
    rows = await store.events(review.id)
    assert all(row["data"].get("name") != "cancelled writer" for row in rows)
    assert rows[-1]["data"]["name"] == "successful writer"
