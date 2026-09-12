import json

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import VerificationResult
from reviewer.findings.validator import read_lines
from reviewer.telemetry.activity import activity


@activity("tool", "Independent verifier")
async def verify(finding, bundle, llm, redactor):
    a = finding.anchor
    lines = read_lines(bundle.code.worktree_path, a.file) or []
    evidence = []
    for e in finding.evidence:
        cited = read_lines(bundle.code.worktree_path, e.file) or []
        # Proposer-authored evidence notes are excluded along with its rationale.
        evidence.append(
            {
                "file": e.file,
                "line_start": e.line_start,
                "code": "\n".join(cited[e.line_start - 1 : e.line_end]),
            }
        )
    payload = {
        "claim": finding.claim,
        "file": a.file,
        "line_start": a.line_start,
        "code": "\n".join(lines[a.line_start - 1 : a.line_end]),
        "evidence": evidence,
    }
    version, prompt, digest = PROMPTS["verification"]
    result = await llm.complete(
        stage="verification",
        tier="verification",
        system=prompt + "\n" + INJECTION_RULE,
        user=frame(redactor.text(json.dumps(payload)), "verification-code"),
        response_model=VerificationResult,
        review_id=bundle.review_id,
        timeout_s=90,
        prompt_version=version,
    )
    finding.verification = result
    if result.verdict == "rejected":
        finding.status = "discarded"
    elif result.verdict == "uncertain":
        finding.status = "downgraded"
    else:
        finding.status = "verified"
    return finding
