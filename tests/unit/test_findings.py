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
