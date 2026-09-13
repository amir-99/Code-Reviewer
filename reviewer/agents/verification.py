import json

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.context.redaction import redact_source
from reviewer.findings.models import VerificationResult
from reviewer.findings.validator import read_lines
from reviewer.telemetry.activity import activity


@activity("tool", "Independent verifier")
async def verify(finding, bundle, llm, redactor):
    a = finding.anchor
    from reviewer.context.excerpts import excerpt, shorten
    from reviewer.services.symbols.index import SymbolIndex

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
        symbols = SymbolIndex()
        symbols.add(path, "\n".join(lines))
        # Scanning precedes this call. Redact full source before inserting line
        # numbers or JSON escapes that would break multiline-secret matching.
        lines = redact_source("\n".join(lines), redactor).split("\n") if lines else []
        value = excerpt(lines, limit=allowance)
        enclosing = [
            s
            for s in symbols.symbols.get(path, [])
            if s["start"] <= start <= end <= s["end"]
        ]
        symbol = (
            min(enclosing, key=lambda s: s["end"] - s["start"]) if enclosing else None
        )
        if not value["complete_file"]:
            first, last = (
                (symbol["start"], symbol["end"])
                if symbol
                else (max(1, start - 40), min(len(lines), end + 40))
            )
            value = excerpt(lines, first, last, allowance)
            if value["line_end"] < end:
                # An oversized enclosing function must not crowd out the claim's
                # actual location. The narrower window is explicitly incomplete.
                value = excerpt(lines, start, min(len(lines), end + 40), allowance)
        value["complete_symbol"] = bool(
            symbol
            and value["line_start"] <= symbol["start"]
            and value["line_end"] >= symbol["end"]
        )
        evidence.append(
            {"file": path, "cited_line_start": start, "cited_line_end": end, **value}
        )
    payload = {"claim": finding.claim, "evidence": evidence}

    def render():
        return frame(redactor.text(json.dumps(payload)), "verification-code")

    system = prompt + "\n" + INJECTION_RULE
    user = render()
    while (
        len(
            json.dumps(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                ensure_ascii=False,
            ).encode()
        )
        > limit
    ):
        candidates = [i for i, value in enumerate(evidence) if value.get("code")]
        if not candidates:
            break
        i = max(candidates, key=lambda i: len(evidence[i]["code"]))
        evidence[i] = shorten(evidence[i])
        evidence[i]["complete_symbol"] = False
        user = render()
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
    result = await llm.complete(
        stage="verification",
        tier="verification",
        system=system,
        user=user,
        response_model=VerificationResult,
        review_id=bundle.review_id,
        timeout_s=180,
        prompt_version=version,
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
