import json
from html import unescape
from types import SimpleNamespace

import pytest
from test_findings import finding
from test_stages import bundle

from reviewer.agents.verification import verify
from reviewer.context.excerpts import excerpt
from reviewer.context.models import DiffLine
from reviewer.context.partition import partition
from reviewer.context.redaction import Redactor
from reviewer.findings.models import Evidence, VerificationResult
from reviewer.findings.noise import select
from reviewer.findings.policy import normalize
from reviewer.orchestrator.verification import verify_findings


def payload(user):
    return json.loads(unescape(user.split("\n", 1)[1].rsplit("\n", 1)[0]))


@pytest.mark.parametrize("kind", ["hunks", "file_group", "file"])
def test_split_function_is_explicitly_an_excerpt_and_continuation_is_retained(
    tmp_path, kind
):
    b = bundle(tmp_path, 1)
    file = b.code.files[0]
    source = (
        ["def reconcile():"]
        + ["    value = 12345678901234567890"] * 140
        + ["    return backend.reconcile()"]
    )
    file.total_lines = len(source)
    file.symbol_ranges = [(1, len(source))]
    file.lines = [
        DiffLine(text=line, new_line=n, old_line=None, kind="added")
        for n, line in enumerate(source, 1)
    ]
    units = partition(b, kind, 6000)
    first = units[0].file_context[file.path]
    assert first["total_lines"] == 142
    assert first["has_more_after"] and not first["complete_file"]
    assert first["chunk_count"] == len(units) > 1
    assert "return backend.reconcile()" in units[-1].content
    assert not units[-1].file_context[file.path]["has_more_after"]
    supplied = [
        line.split("added: ", 1)[1]
        for unit in units
        for line in unit.content.splitlines()
    ]
    assert supplied == source


def test_fitting_function_stays_together_despite_preceding_code(tmp_path):
    b = bundle(tmp_path, 1)
    file = b.code.files[0]
    source = (
        ["# heading " + "x" * 40] * 3
        + [
            "def f():",
            "    a = 1",
            "    b = 2",
            "    return a + b",
        ]
        + ["# trailing " + "x" * 40] * 3
    )
    file.symbol_ranges = [(4, 7)]
    file.total_lines = len(source)
    file.lines = [
        DiffLine(text=line, new_line=n, old_line=None, kind="added")
        for n, line in enumerate(source, 1)
    ]
    units = partition(b, "hunks", 256)
    containing = next(u for u in units if "def f():" in u.content)
    assert "return a + b" in containing.content
    assert all(len(u.content.encode()) <= 256 for u in units)


def test_requested_continuation_has_real_numbers_and_explicit_budget_boundary():
    lines = ["padding " * 20] * 100 + ["return nil", "}"]
    first = excerpt(lines, limit=6000)
    assert not first["complete_file"] and first["has_more_after"]
    assert first["line_end"] < 100
    tail = excerpt(lines, 101, 102, 6000)
    assert tail["code"] == "101: return nil\n102: }"
    assert tail["range_complete"] and not tail["has_more_after"]
    assert tail["has_more_before"] and not tail["complete_file"]


@pytest.mark.parametrize(
    "source,claim,scope,anchor,proof",
    [
        (
            "package memory\nfunc Reconcile() error {\n"
            + "\n".join("// padding" for _ in range(108))
            + "\nbackend.Reconcile()\nreturn nil\n}\n",
            "Reconcile lacks backend delegation and return",
            "symbol",
            2,
            "backend.Reconcile()",
        ),
        (
            "package outbox\nfunc TestSweep(t *testing.T) {\n"
            "sweeper.Sweep()\nif !reflect.DeepEqual(attempts, []int{1, 2}) { t.Fatal(attempts) }\n}\n",
            "TestSweep lacks assertions and closing braces",
            "symbol",
            2,
            "reflect.DeepEqual",
        ),
        (
            "package workflow\nfunc validate() error {\ncheckFlags()\nreturn nil\n}\n",
            "validate lacks a terminating return",
            "symbol",
            2,
            "return nil",
        ),
        (
            'package store\nimport "fmt"\nfunc f() { fmt.Println("used") }\n',
            "Unused fmt import prevents compilation",
            "file",
            2,
            "fmt.Println",
        ),
    ],
)
async def test_verifier_sees_continuation_and_can_reject_false_absence(
    tmp_path, source, claim, scope, anchor, proof
):
    (tmp_path / "example.go").write_text(source)
    f = finding(anchor)
    f.anchor.file = "example.go"
    f.claim, f.evidence_scope = claim, scope
    f.evidence = [
        Evidence(
            file="example.go",
            line_start=anchor,
            line_end=anchor,
            note="persuasive rationale",
        )
    ]
    f.reason = "proposer persuasion"

    class LLM:
        async def complete(self, **kwargs):
            data = payload(kwargs["user"])
            assert proof in data["evidence"][0]["code"]
            assert data["evidence"][0]["complete_file"]
            assert "persuasive" not in kwargs["user"]
            assert "proposer persuasion" not in kwargs["user"]
            assert "evidence_scope" not in data
            return VerificationResult(
                verdict="rejected",
                counterargument="Continuation disproves claim",
                reasoning="Code exists",
            )

    await verify(f, bundle(tmp_path, 1), LLM(), Redactor())
    normalize(f, {"example.go"})
    assert f.status == "discarded"
    assert select([f])[:2] == ([], [])


async def test_incomplete_file_cannot_confirm_absence_even_if_model_says_confirmed(
    tmp_path,
):
    (tmp_path / "f0.py").write_text(
        "import os\n" + "# padding\n" * 15000 + "os.getcwd()\n"
    )
    f = finding()
    f.evidence_scope = "file"

    class LLM:
        specs = {"verification": SimpleNamespace(context_tokens=16000)}

        async def complete(self, **kwargs):
            assert not payload(kwargs["user"])["evidence"][0]["complete_file"]
            size = len(
                json.dumps(
                    [
                        {"role": "system", "content": kwargs["system"]},
                        {"role": "user", "content": kwargs["user"]},
                    ],
                    ensure_ascii=False,
                ).encode()
            )
            assert size <= 12000
            return VerificationResult(
                verdict="confirmed", counterargument="none", reasoning="Unused"
            )

    await verify(f, bundle(tmp_path, 1), LLM(), Redactor())
    normalize(f, {"f0.py"})
    assert f.verification.verdict == "uncertain"
    assert f.status == "suppressed"
    assert select([f])[:2] == ([], [])


@pytest.mark.parametrize("verdict", ["confirmed", "uncertain", None])
def test_scope_claims_need_verification_but_local_policy_is_preserved(verdict):
    f = finding()
    f.evidence_scope = "symbol"
    f.anchor.in_diff = f.anchor.introduced_by_this_change = True
    f.severity_proposed = "REQUIRED"
    if verdict:
        f.verification = VerificationResult(
            verdict=verdict, counterargument="none", reasoning="evidence"
        )
    normalize(f, {"f0.py"})
    if verdict == "confirmed":
        assert f.severity_final == "REQUIRED"
        assert select([f])[0] == [f]
    else:
        assert f.status == "suppressed"
        local = finding()
        normalize(local, {"f0.py"})
        assert local.status != "suppressed"


async def test_unavailable_scan_prevents_expanded_source_exposure(tmp_path):
    f = finding()
    f.evidence_scope = "file"

    async def provider(requests):
        return {"f0.py": {"unavailable": True}}

    class Forbidden:
        async def complete(self, **kwargs):
            pytest.fail("A failed scan must prevent verification")

    await verify_findings(
        [f], bundle(tmp_path, 1), Forbidden(), Redactor(), provider, 1
    )
    normalize(f, {"f0.py"})
    assert f.verification.verdict == "uncertain"
    assert f.status == "suppressed"


async def test_builder_retrieves_symbol_and_range_beyond_old_prefix(tmp_path):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from reviewer.config.schema import ProjectConfig
    from reviewer.context.builder import build
    from reviewer.context.models import ChangedFile
    from reviewer.findings.models import ContextRequest
    from reviewer.services.forge.gitlab import MergeRequestContext
    from reviewer.services.secrets.scanner import FakeSecretScanner

    source = "# padding\n" * 1000 + "def finish():\n    return 42\n"
    changed = ChangedFile(
        path="large.py",
        change_type="modified",
        lines=[
            DiffLine(text="    return 42", new_line=1002, old_line=1002, kind="context")
        ],
    )
    git = SimpleNamespace(
        merge_base=AsyncMock(return_value="b" * 40),
        diff=AsyncMock(return_value=[changed]),
        command=AsyncMock(return_value=b""),
        read_file=AsyncMock(return_value=source),
    )
    mr = MergeRequestContext(project_id=1, iid=1, head_sha="a" * 40)
    result = await build(
        SimpleNamespace(id=uuid4(), head_sha=mr.head_sha, started_at=datetime.now(UTC)),
        mr,
        SimpleNamespace(path=tmp_path, mirror=tmp_path),
        git,
        SimpleNamespace(get_changed_paths=AsyncMock(return_value=["large.py"])),
        None,
        None,
        FakeSecretScanner(),
        ProjectConfig(),
    )
    b, _, _, _, provider = result
    assert b.code.files[0].total_lines == 1002
    assert b.code.files[0].symbol_ranges == [(1001, 1002)]
    symbol = await provider(
        [ContextRequest(kind="symbol", target="finish", reason="Need complete body")]
    )
    assert symbol["large.py"]["code"] == "1001: def finish():\n1002:     return 42"
    assert symbol["large.py"]["range_complete"]
    tail = await provider(
        [
            ContextRequest(
                kind="file",
                target="large.py",
                reason="Read continuation",
                line_start=1002,
                line_end=1002,
            )
        ]
    )
    assert tail["large.py"]["code"] == "1002:     return 42"
    # The symbol index reads once, then the cache scans and shares one full read.
    assert git.read_file.await_count == 2


async def test_requested_context_is_sized_for_receiving_model_with_truthful_metadata(
    tmp_path,
):
    from reviewer.agents.base import TemplateAgent
    from reviewer.context.partition import WorkUnit
    from reviewer.findings.models import ContextRequest, Coverage, StageEnvelope

    b = bundle(tmp_path, 1)
    agent = TemplateAgent("correctness")
    agent.context_tokens = 18000
    calls = []

    class Model:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return StageEnvelope(
                findings=[],
                context_requests=[
                    ContextRequest(kind="file", target="peer.py", reason="Need body")
                ]
                if len(calls) == 1
                else [],
                coverage=Coverage(
                    units_examined=["f0.py:unit-0"], units_skipped=[], skip_reason=None
                ),
            )

    async def provider(requests):
        return {"peer.py": excerpt(['# < > & " quoted ' * 20] * 1000)}

    await agent.run(
        b,
        WorkUnit(id="f0.py:unit-0", paths=["f0.py"], content="new=1 added: x=1"),
        Model(),
        provider,
    )
    final = calls[-1]
    size = len(
        json.dumps(
            [
                {"role": "system", "content": final["system"]},
                {"role": "user", "content": final["user"]},
            ],
            ensure_ascii=False,
        ).encode()
    )
    assert size <= 13500
    context = payload(
        final["user"].split('<untrusted_data source="requested-code-context"', 1)[1]
    )
    assert not context["peer.py"]["complete_file"]
    assert context["peer.py"]["has_more_after"]
    assert context["peer.py"]["line_end"] < 1000


async def test_expanded_verifier_redacts_multiline_secret_before_numbering(tmp_path):
    secret = "private-first-line\nprivate-second-line"
    (tmp_path / "f0.py").write_text("value = '''" + secret + "'''\nprint(value)\n")
    f = finding(3)
    f.evidence = [Evidence(file="f0.py", line_start=3, line_end=3, note="usage")]
    redactor = Redactor([{"Secret": secret, "RuleID": "multiline"}])

    class LLM:
        async def complete(self, **kwargs):
            assert "private-first-line" not in kwargs["user"]
            assert "private-second-line" not in kwargs["user"]
            data = payload(kwargs["user"])
            assert data["evidence"][0]["total_lines"] == 3
            assert "3: print(value)" in data["evidence"][0]["code"]
            return VerificationResult(
                verdict="confirmed", counterargument="none", reasoning="Supported"
            )

    await verify(f, bundle(tmp_path, 1), LLM(), redactor)
    assert f.verified


async def test_actual_missing_return_can_still_be_confirmed(tmp_path):
    (tmp_path / "example.go").write_text(
        "package example\nfunc broken() error {\nprintln(1)\n}\n"
    )
    f = finding(2)
    f.anchor.file = "example.go"
    f.evidence = [Evidence(file="example.go", line_start=2, line_end=3, note="body")]
    f.evidence_scope = "symbol"

    class LLM:
        async def complete(self, **kwargs):
            data = payload(kwargs["user"])
            assert all(e["complete_file"] for e in data["evidence"])
            assert "4: }" in data["evidence"][0]["code"]
            return VerificationResult(
                verdict="confirmed",
                counterargument="none",
                reasoning="Complete function lacks required return",
            )

    await verify(f, bundle(tmp_path, 1), LLM(), Redactor())
    normalize(f, {"example.go"})
    assert f.verified and f.status != "suppressed"
