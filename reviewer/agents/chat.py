"""Questions about a finished review, answered from its stored record.

The chat is not a stage and not an agent framework: it is a bounded loop of at
most `1 + context_rounds` gateway calls. The model may ask for material from a
fixed menu; code fetches it, frames it as untrusted, and asks once more. Code
also owns the citations: a citation naming something the model was never given
is dropped, never shown.
"""

import json
from typing import Literal

from pydantic import Field

from reviewer.agents.base import PROMPTS
from reviewer.context.framing import frame
from reviewer.findings.models import Schema
from reviewer.telemetry.activity import activity

PROMPT_LIMIT_RATIO = 0.7


class ChatContextRequest(Schema):
    kind: Literal["file", "symbol", "issue", "page", "events", "comments"]
    target: str = Field(default="", max_length=300)
    reason: str = Field(default="", max_length=300)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class Citation(Schema):
    kind: Literal["file", "finding", "issue", "page"]
    ref: str = Field(max_length=300)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)


class ChatTurn(Schema):
    """One model reply: either an answer, or a request for more material."""

    context_requests: list[ChatContextRequest] = Field(max_length=5, default=[])
    answer: str | None = Field(default=None, max_length=6000)
    citations: list[Citation] = Field(max_length=20, default=[])


class ChatAnswer(Schema):
    """The final round: an answer is required."""

    answer: str = Field(min_length=1, max_length=6000)
    citations: list[Citation] = Field(max_length=20, default=[])


class ChatFailed(RuntimeError):
    pass


def validate_citations(citations, given):
    """Only citations naming material the model was actually given survive."""
    kept = []
    for citation in citations:
        refs = given.get(citation.kind, set())
        if citation.ref in refs:
            kept.append(citation.model_dump(mode="json"))
    return kept


@activity("tool", "Review chat")
async def answer(sources, question, llm, config, review_id, spec, prompt_limit=None):
    """Run the bounded loop and return (answer, citations, context_used, model).

    The prompt is sized against the receiving model's window and, when given,
    against what the chat budget can still reserve: the gateway counts prompt
    bytes conservatively before it sends, so an oversized prompt is refused
    before it costs anything, and this loop must not build one.
    """
    version, prompt, _digest = PROMPTS["chat"]
    context = await sources.fixed()
    context["requested"] = await sources.prefetch(question)
    rounds = 1 + config.chat.context_rounds
    limit = int(spec.context_tokens * PROMPT_LIMIT_RATIO)
    if prompt_limit is not None:
        limit = max(1, min(limit, int(prompt_limit)))
    for round_no in range(rounds):
        final = round_no == rounds - 1
        user = sources.render(context, question, limit)
        result = await llm.complete(
            stage="chat",
            tier="chat",
            system=prompt,
            user=user,
            response_model=ChatAnswer if final else ChatTurn,
            review_id=review_id,
            timeout_s=config.chat.timeout_s,
            prompt_version=version,
        )
        requests = [] if final else result.context_requests
        if result.answer or not requests:
            if not result.answer:
                raise ChatFailed("The model neither answered nor asked")
            return (
                result.answer,
                validate_citations(result.citations, sources.given()),
                sources.used(),
                spec.model,
            )
        extra = await sources.provide(requests)
        if not extra or all(
            context["requested"].get(key) == value for key, value in extra.items()
        ):
            # Nothing new can arrive; the next round must answer.
            rounds = round_no + 2
        context["requested"].update(extra)
    raise ChatFailed("The model did not answer")


def render_untrusted(payload, source):
    return frame(json.dumps(payload, ensure_ascii=False, default=str), source)
