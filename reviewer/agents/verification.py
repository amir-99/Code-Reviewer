import json

from pydantic import Field

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.context.partition import render
from reviewer.context.redaction import redact_source
from reviewer.findings.models import ContextRequest, VerificationResult
from reviewer.findings.validator import read_lines
from reviewer.telemetry.activity import activity

# A file this short is sent whole: cheaper than being wrong about its scope.
SMALL_FILE_LINES = 150
# An enclosing definition longer than this is not a useful local scope.
LOCAL_SCOPE_LINES = 300
CHANGE_MARGIN = 10


class VerifierResponse(VerificationResult):
    # After the verdict, so the model rules before it asks for more.
    context_requests: list[ContextRequest] = Field(default=[], max_length=2)


def change_near(bundle, path, start, end, margin=CHANGE_MARGIN):
    """What this merge request changed around a citation, as diff lines."""
    file = next((f for f in bundle.code.files if f.path == path), None)
    if file is None:
        return None
    selected, following = [], None
    for line in reversed(file.lines):
        if line.new_line is not None:
            following = line.new_line
        position = following
        if position is not None and start - margin <= position <= end + margin:
            selected.append(line)
    selected.reverse()
    if not any(line.kind != "context" for line in selected):
        return None
    return "\n".join(render(line) for line in selected)


def scoped(lines, symbol, start, end, evidence_scope, allowance):
    """The source a verifier needs for one citation, within its allowance.

    File-wide claims, and files short enough to be cheap, are sent whole when
    they fit. A symbol claim needs its complete enclosing definition. A local
    claim needs the enclosing definition when it is a reasonable size, and a
    window around the citation otherwise.
    """
    from reviewer.context.excerpts import excerpt

    if evidence_scope == "file" or len(lines) <= SMALL_FILE_LINES:
        value = excerpt(lines, limit=allowance)
        if value["complete_file"]:
            return value
    if symbol and (
        evidence_scope == "symbol"
        or symbol["end"] - symbol["start"] <= LOCAL_SCOPE_LINES
    ):
        first, last = symbol["start"], symbol["end"]
    else:
        first, last = max(1, start - 40), min(len(lines), end + 40)
    value = excerpt(lines, first, last, allowance)
    if value["line_end"] < end:
        # An oversized enclosing function must not crowd out the claim's
        # actual location. The narrower window is explicitly incomplete.
        value = excerpt(lines, start, min(len(lines), end + 40), allowance)
    return value


@activity("tool", "Independent verifier")
async def verify(finding, bundle, llm, redactor, context_provider=None):
    a = finding.anchor
    from reviewer.context.excerpts import shorten
    from reviewer.services.symbols.index import definitions

    version, prompt, digest = PROMPTS["verification"]
    spec = getattr(llm, "specs", {}).get("verification")
    limit = spec.context_tokens * 3 // 4 if spec else 64000
    locations = [(a.file, a.line_start, a.line_end)] + [
        (e.file, e.line_start, e.line_end) for e in finding.evidence
    ]
    locations = list(dict.fromkeys(locations))
    evidence = []
    for path, start, end in locations:
        lines = read_lines(bundle.code.worktree_path, path)
        if lines is None:
            evidence.append({"file": path, "unavailable": True})
            continue
        allowance = max(0, (limit - 4000) // max(1, len(locations)))
        enclosing = [
            s
            for s in definitions(path, "\n".join(lines))
            if s["start"] <= start <= end <= s["end"]
        ]
        symbol = (
            min(enclosing, key=lambda s: s["end"] - s["start"]) if enclosing else None
        )
        # Scanning precedes this call. Redact full source before inserting line
        # numbers or JSON escapes that would break multiline-secret matching.
        lines = redact_source("\n".join(lines), redactor).split("\n") if lines else []
        value = scoped(lines, symbol, start, end, finding.evidence_scope, allowance)
        value["complete_symbol"] = bool(
            symbol
            and value["line_start"] <= symbol["start"]
            and value["line_end"] >= symbol["end"]
        )
        item = {"file": path, "cited_line_start": start, "cited_line_end": end}
        change = change_near(bundle, path, start, end)
        if change:
            item["change"] = change
        evidence.append(item | value)
    payload = {"claim": finding.claim, "evidence": evidence}
    context = {}

    def render_user():
        user = frame(redactor.text(json.dumps(payload)), "verification-code")
        if context:
            user += "\n" + frame(
                redactor.text(json.dumps(context)), "requested-code-context"
            )
        return user

    def size(user):
        return len(
            json.dumps(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                ensure_ascii=False,
            ).encode()
        )

    def fit():
        user = render_user()
        while size(user) > limit:
            requested = [k for k, v in context.items() if v.get("code")]
            if requested:
                # Requested context yields before the cited evidence does.
                key = max(requested, key=lambda k: len(context[k]["code"]))
                context[key] = shorten(context[key])
            else:
                candidates = [i for i, v in enumerate(evidence) if v.get("code")]
                if not candidates:
                    break
                i = max(candidates, key=lambda i: len(evidence[i]["code"]))
                evidence[i] = shorten(evidence[i])
                evidence[i]["complete_symbol"] = False
            user = render_user()
        return user

    system = prompt + "\n" + INJECTION_RULE
    user = fit()
    scope_complete = all(
        value.get("complete_file")
        or (finding.evidence_scope == "symbol" and value.get("complete_symbol"))
        for value in evidence
    )
    citations_complete = all(
        not value.get("unavailable")
        and value["line_start"] <= value["cited_line_start"]
        and value["line_end"] >= value["cited_line_end"]
        for value in evidence
    )

    async def ask(user):
        return await llm.complete(
            stage="verification",
            tier="verification",
            system=system,
            user=user,
            response_model=VerifierResponse,
            review_id=bundle.review_id,
            timeout_s=180,
            prompt_version=version,
        )

    response = await ask(user)
    requests = getattr(response, "context_requests", [])
    if response.verdict == "uncertain" and requests and context_provider:
        # One round only, and only for a claim the cited code cannot settle:
        # the code a claim depends on often lives in a callee or a caller.
        extra = await context_provider(requests)
        context.update(
            {
                key: value
                for key, value in (extra or {}).items()
                if isinstance(value, dict) and value.get("code")
            }
        )
        if context:
            response = await ask(fit())
    result = VerificationResult(
        counterargument=response.counterargument,
        reasoning=response.reasoning,
        verdict=response.verdict,
    )
    if (
        not citations_complete
        or finding.evidence_scope != "local"
        and not scope_complete
    ) and result.verdict == "confirmed":
        result = VerificationResult(
            verdict="uncertain",
            counterargument="The required source scope was not supplied completely",
            reasoning="An incomplete excerpt cannot establish absence",
        )
    finding.verification = result
    if result.verdict == "rejected":
        finding.status = "discarded"
    elif result.verdict == "uncertain":
        finding.status = "downgraded"
    else:
        finding.status = "verified"
    return finding
