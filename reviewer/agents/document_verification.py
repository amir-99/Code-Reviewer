"""Independent verification of one document finding.

The verifier sees the claim and the cited passages in their sections, never
the proposer's rationale, confidence or severity.
"""

import json

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import INJECTION_RULE, frame
from reviewer.findings.document import locate
from reviewer.findings.models import VerificationResult
from reviewer.telemetry.activity import activity

STAGE = "document_verification"
CONTEXT_CHARS = 1500


def passage(corpus, anchor, allowance):
    section = locate(corpus, anchor)
    if section is None:
        return {"page_id": anchor.page_id, "unavailable": True}
    text = section.text
    if len(text) > allowance:
        low, needle = text.lower(), anchor.quote.strip().lower()
        at = low.find(needle[:60])
        start = max(0, (at if at >= 0 else 0) - CONTEXT_CHARS)
        text = text[start : start + allowance]
        complete = False
    else:
        complete = not section.truncated
    page = corpus.page(anchor.page_id)
    return {
        "page_id": anchor.page_id,
        "title": page.title if page else None,
        "heading_path": section.heading_path,
        "quote": anchor.quote,
        "section_text": text,
        "complete_section": complete,
    }


@activity("tool", "Independent verifier")
async def verify(finding, corpus, llm, review_id, redactor, spec=None):
    version, prompt, _digest = PROMPTS[STAGE]
    limit = int((spec.context_tokens if spec and spec.context_tokens else 64000) * 0.75)
    anchors = [finding.anchor] + list(finding.related)
    allowance = max(600, (limit - 4000) // max(1, len(anchors)))
    payload = {
        "claim": finding.claim,
        "category": finding.category,
        "passages": [passage(corpus, a, allowance) for a in anchors],
    }
    system = prompt + "\n" + INJECTION_RULE
    user = frame(
        redactor.text(json.dumps(payload, ensure_ascii=False)), "verification-text"
    )
    result = await llm.complete(
        stage=STAGE,
        tier=STAGE,
        system=system,
        user=user,
        response_model=VerificationResult,
        review_id=review_id,
        timeout_s=180,
        prompt_version=version,
    )
    if result.verdict == "confirmed" and any(
        p.get("unavailable") for p in payload["passages"]
    ):
        result = VerificationResult(
            verdict="uncertain",
            counterargument="A cited passage was not supplied",
            reasoning="An absent passage cannot establish the claim",
        )
    if (
        result.verdict == "confirmed"
        and finding.category == "completeness"
        and not all(p.get("complete_section") for p in payload["passages"])
    ):
        result = VerificationResult(
            verdict="uncertain",
            counterargument="The cited section was not supplied completely",
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
