import json

from test_findings import finding
from test_stages import bundle

from reviewer.agents.verification import VerifierResponse, verify
from reviewer.context.models import DiffLine
from reviewer.context.redaction import Redactor
from reviewer.findings.models import ContextRequest, Evidence


def payload(user):
    body = user.split('source="verification-code" trust="none">\n', 1)[1]
    return json.loads(body.split("\n</untrusted_data>", 1)[0])


def large_module(tmp_path):
    lines = [f"# filler {n}" for n in range(400)] + [
        "def charge(amount):",
        "    total = amount * 2",
        "    return total",
    ]
    lines += [f"# trailer {n}" for n in range(400)]
    (tmp_path / "f0.py").write_text("\n".join(lines) + "\n")
    return lines.index("    total = amount * 2") + 1


def cited(line):
    f = finding(line)
    f.evidence = [Evidence(file="f0.py", line_start=line, line_end=line, note="")]
    return f


class Answers:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def answer(verdict, requests=()):
    return VerifierResponse(
        counterargument="c",
        reasoning="r",
        verdict=verdict,
        context_requests=list(requests),
    )


async def test_local_claim_in_a_large_file_gets_its_function_and_the_change(tmp_path):
    line = large_module(tmp_path)
    b = bundle(tmp_path, 1)
    b.code.files[0].lines = [
        DiffLine(
            text="    total = amount", new_line=None, old_line=line, kind="removed"
        ),
        DiffLine(
            text="    total = amount * 2", new_line=line, old_line=None, kind="added"
        ),
    ]
    llm = Answers(answer("confirmed"))
    await verify(cited(line), b, llm, Redactor())
    (evidence,) = payload(llm.calls[0]["user"])["evidence"]
    assert (evidence["line_start"], evidence["line_end"]) == (line - 1, line + 1)
    assert not evidence["complete_file"] and evidence["complete_symbol"]
    assert "filler" not in evidence["code"]
    assert evidence["change"].splitlines() == [
        "      -     total = amount",
        f"  {line} +     total = amount * 2",
    ]


async def test_uncertain_verdict_may_ask_once_for_the_code_it_depends_on(tmp_path):
    line = large_module(tmp_path)
    requested = []

    async def provider(requests):
        requested.append(requests)
        return {
            "billing.py": {
                "code": "1: def double(x):\n2:     return x * 3",
                "line_start": 1,
                "line_end": 2,
            }
        }

    llm = Answers(
        answer(
            "uncertain", [ContextRequest(kind="symbol", target="double", reason="r")]
        ),
        answer("confirmed"),
    )
    f = await verify(cited(line), bundle(tmp_path, 1), llm, Redactor(), provider)
    assert len(llm.calls) == 2 and len(requested) == 1
    assert "return x * 3" in llm.calls[1]["user"]
    assert "return x * 3" not in llm.calls[0]["user"]
    assert f.verification.verdict == "confirmed" and f.status == "verified"
    assert not hasattr(f.verification, "context_requests")


async def test_settled_verdicts_never_spend_a_second_call(tmp_path):
    line = large_module(tmp_path)
    llm = Answers(
        answer(
            "rejected", [ContextRequest(kind="callers", target="charge", reason="r")]
        )
    )

    async def provider(requests):
        raise AssertionError("A settled verdict must not request context")

    f = await verify(cited(line), bundle(tmp_path, 1), llm, Redactor(), provider)
    assert len(llm.calls) == 1 and f.status == "discarded"
