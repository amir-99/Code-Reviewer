from uuid import uuid4

import pytest
from test_stages import bundle

from reviewer.findings.dedup import fingerprint
from reviewer.findings.models import Anchor, Evidence, Finding, Provenance
from reviewer.findings.policy import normalize
from reviewer.findings.validator import validate
from reviewer.services.symbols.index import SymbolIndex


def finding(line=1):
    return Finding(
        id=str(uuid4()),
        fingerprint="a" * 32,
        stage="correctness",
        provenance=Provenance(
            agent="correctness",
            prompt_version="1.0.0",
            model="fake",
            run_id="run",
            context_bundle_hash="hash",
        ),
        anchor=Anchor(file="f0.py", line_start=line, line_end=line),
        category="correctness",
        severity_proposed="BLOCKER",
        claim="Fails at runtime",
        reason="Wrong result",
        impact="Wrong result",
        failure_scenario="Input is zero",
        evidence=[Evidence(file="f0.py", line_start=1, line_end=1, note="code")],
        suggested_direction="Handle zero",
        confidence="high",
    )


async def test_fabricated_anchor_discarded_and_context_is_fyi(tmp_path):
    (tmp_path / "f0.py").write_text("x=1\ny=2\n")
    b = bundle(tmp_path, 1)
    f = await validate(finding(999), b, SymbolIndex())
    assert f.status == "discarded"
    f = await validate(finding(2), b, SymbolIndex())
    normalize(f, {"f0.py"})
    assert f.severity_final == "FYI" and not f.anchor.introduced_by_this_change


@pytest.mark.parametrize(
    "claim", ["Error on 12 items!", "ERROR on 99 items.", "error on items"]
)
def test_fingerprint_normalization(claim):
    assert fingerprint(1, "a.py", "correctness", claim) == fingerprint(
        1, "a.py", "correctness", "error on items"
    )


@pytest.mark.parametrize("level", ["CRITICAL", "HIGH", "MEDIUM", "LOW", None])
def test_impact_never_changes_policy_or_inline_priority(level):
    from types import SimpleNamespace

    from reviewer.config.schema import ProjectConfig
    from reviewer.decision.engine import decide
    from reviewer.findings.models import VerificationResult
    from reviewer.findings.noise import select

    f = finding()
    f.impact_level = level
    f.anchor.in_diff = f.anchor.introduced_by_this_change = True
    f.verification = VerificationResult(
        counterargument="none", reasoning="proven", verdict="confirmed"
    )
    normalize(f, {"f0.py"})
    assert f.severity_final == "REQUIRED"
    review = SimpleNamespace(partial=False, complete=True)
    assert decide(review, [f], [], ProjectConfig()) == "REQUEST_CHANGES"
    f.category = "style"
    normalize(f, {"f0.py"})
    assert f.severity_final == "NIT"
    assert decide(review, [f], [], ProjectConfig()) == "APPROVE"
    inline, summary, _ = select([f])
    assert not inline and summary == [f]


def test_impact_roundtrip_legacy_and_strict_contract():
    from pydantic import ValidationError

    from reviewer.api.admin import summarise
    from reviewer.findings.models import StageEnvelope
    from reviewer.services.llm.client import strict_schema

    f = finding()
    old = f.model_dump(mode="json")
    old.pop("impact_level")
    assert Finding.model_validate(old).impact_level is None
    assert summarise(old)["impact_level"] is None
    f.impact_level = "HIGH"
    stored = f.model_dump(mode="json")
    assert Finding.model_validate(stored).impact_level == "HIGH"
    assert summarise(stored)["impact_level"] == "HIGH"
    with pytest.raises(ValidationError):
        f.impact_level = "EXTREME"
    schema = strict_schema(StageEnvelope.model_json_schema())
    proposal = schema["$defs"]["ProposedFinding"]
    assert "impact_level" in proposal["required"]
    assert {"type": "null"} in proposal["properties"]["impact_level"]["anyOf"]


def test_dedup_retains_impact_with_claim_and_provenance():
    from reviewer.findings.dedup import deduplicate

    first, other = finding(), finding()
    first.impact_level = "LOW"
    other.impact_level = "CRITICAL"
    other.claim = "Different nearby failure"
    other.impact = "Broad outage"
    other.fingerprint = "b" * 32
    assert deduplicate([first, other]) == [first]
    assert first.impact_level == "LOW"
    assert first.impact == "Wrong result"


async def test_verifier_does_not_receive_impact_level(tmp_path):
    from reviewer.agents.verification import verify
    from reviewer.findings.models import VerificationResult

    class LLM:
        async def complete(self, **kwargs):
            assert "impact_level" not in kwargs["user"]
            assert "CRITICAL" not in kwargs["user"]
            return VerificationResult(
                counterargument="none", reasoning="proven", verdict="confirmed"
            )

    class Redactor:
        def text(self, value):
            return value

    (tmp_path / "f0.py").write_text("x=1\n")
    f = finding()
    f.impact_level = "CRITICAL"
    await verify(f, bundle(tmp_path, 1), LLM(), Redactor())
    assert f.verified


def test_impact_does_not_displace_higher_disposition_at_inline_cap():
    from reviewer.findings.noise import select

    required, suggestion = finding(), finding()
    for f in (required, suggestion):
        f.anchor.in_diff = f.anchor.introduced_by_this_change = True
    required.severity_final, required.impact_level = "REQUIRED", "LOW"
    suggestion.severity_final, suggestion.impact_level = "SUGGESTION", "CRITICAL"
    inline, _, overflow = select([suggestion, required], max_inline=1)
    assert inline == [required]
    assert overflow == {"correctness": 1}
