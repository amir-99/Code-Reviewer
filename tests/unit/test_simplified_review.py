import json

import pytest
from test_stages import bundle

from reviewer.agents.defects import DefectEnvelope
from reviewer.config.schema import ProjectConfig
from reviewer.context.models import DiffLine, Hunk
from reviewer.context.partition import partition
from reviewer.orchestrator.pipeline import frozen_config
from reviewer.orchestrator.stages import execute


def response(unit_id, **extra):
    return DefectEnvelope.model_validate(
        dict(
            findings=[],
            findings_truncated=False,
            coverage=dict(units_examined=[unit_id], units_skipped=[], skip_reason=None),
            **extra,
        )
    )


async def test_compact_contract_context_and_cap_are_conservative(tmp_path):
    b = bundle(tmp_path, 1)
    calls, reads = [], []

    class Model:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            result = response(
                "f0.py:unit-0",
                context_requests=[
                    dict(kind="file", target="peer.py", reason="Check caller")
                ],
            )
            result.findings_truncated = True
            return result

    async def provider(requests):
        reads.append(requests)
        return {"peer.py": "provided-context"}

    result = await execute("defect_review", b, Model(), ProjectConfig(), provider)
    assert len(calls) == 2 and len(reads) == 1
    assert "provided-context" in calls[-1]["user"]
    assert result.examined == ["f0.py:unit-0"] and result.partial
    schema = json.dumps(calls[0]["response_model"].model_json_schema())
    assert "commit_sha" not in schema and "context_hash" not in schema
    assert "impact_level" in schema


def test_hunks_pack_intact_and_oversized_hunks_split_at_lines(tmp_path):
    b = bundle(tmp_path, 1)
    file = b.code.files[0]
    file.lines = [
        DiffLine(text="é" * 20, new_line=i, old_line=None, kind="added")
        for i in range(1, 9)
    ]
    file.hunks = [
        Hunk(old_start=0, old_lines=0, new_start=1, new_lines=4, header="first"),
        Hunk(old_start=0, old_lines=0, new_start=5, new_lines=4, header="second"),
    ]
    units = partition(b, "hunks", 300)
    assert len(units) == 2
    for index, unit in enumerate(units):
        assert len(unit.content.encode()) <= 300
        for line in file.lines[index * 4 : index * 4 + 4]:
            assert f"new={line.new_line} added: {line.text}" in unit.content
    file.hunks = []
    units = partition(b, "hunks", 256)
    assert all(len(u.content.encode()) <= 256 for u in units)
    assert sum(len(u.content.splitlines()) for u in units) == 8


async def test_oversized_line_is_skipped_without_false_coverage(tmp_path):
    b = bundle(tmp_path, 1)
    b.code.files[0].lines[0].text = "x" * 1000

    class Forbidden:
        async def complete(self, **kwargs):
            pytest.fail("Oversized line must not be sent or claimed examined")

    config = ProjectConfig(review={"unit_tokens": 256})
    result = await execute("defect_review", b, Forbidden(), config)
    assert result.partial and result.skipped and not result.examined


def test_old_frozen_runs_retain_deep_mode():
    assert ProjectConfig().analysis_mode == "standard"
    assert frozen_config({}).analysis_mode == "deep"
    assert frozen_config({"analysis_mode": "standard"}).analysis_mode == "standard"


def test_compact_claim_expands_with_impact_and_code_owned_metadata():
    raw = dict(
        anchor=dict(file="a.py", line_start=1, line_end=1, symbol=None),
        category="correctness",
        claim="Unchecked input",
        failure_scenario="A negative input reaches the write",
        impact="Invalid data saved",
        impact_level="HIGH",
        evidence=[dict(file="a.py", line_start=1, line_end=1)],
        suggested_direction="Validate before writing",
        confidence="high",
        requirement_ref=None,
    )
    envelope = DefectEnvelope.model_validate(
        dict(
            findings=[raw],
            findings_truncated=False,
            coverage=dict(
                units_examined=["a.py:unit-0"], units_skipped=[], skip_reason=None
            ),
        )
    )
    finding = envelope.expand().findings[0]
    assert finding.impact_level == "HIGH"
    assert finding.reason == raw["failure_scenario"]
    assert finding.anchor.commit_sha == "" and not finding.anchor.in_diff
    assert finding.evidence[0].file == "a.py"
    assert finding.evidence[0].note == ""
