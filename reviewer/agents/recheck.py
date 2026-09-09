import json

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.models import RecheckResult
from reviewer.telemetry.activity import activity


@activity("tool", "Fix recheck")
async def judge(finding, before, after, patch, author_notes, llm, redactor, review_id):
    """Judge one published finding against a newer head.

    The judge sees the claim and three views of the code — as it stood when the
    comment was posted, as it stands now, and the diff between them — plus the
    author's replies on the thread. It never sees the severity or confidence the
    proposing stage assigned, for the same reason verification does not.
    """
    payload = {
        "claim": finding.claim,
        "reason": finding.reason,
        "category": finding.category,
        "file": finding.anchor.file,
        "suggested_direction": finding.suggested_direction,
        "cited_evidence": [
            {
                "file": e.file,
                "line_start": e.line_start,
                "line_end": e.line_end,
                "note": e.note,
            }
            for e in finding.evidence
        ],
        "code_when_posted": before,
        "code_at_new_head": after,
        "file_diff_since": patch,
        "author_replies": author_notes,
    }
    version, prompt, digest = PROMPTS["recheck"]
    return await llm.complete(
        stage="recheck",
        tier="recheck",
        system=prompt + "\n" + INJECTION_RULE,
        user=frame(redactor.text(json.dumps(payload)), "recheck-code"),
        response_model=RecheckResult,
        review_id=review_id,
        max_tokens=2000,
        timeout_s=90,
        prompt_version=version,
    )
