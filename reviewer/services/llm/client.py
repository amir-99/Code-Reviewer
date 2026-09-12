"""Audited, budgeted, schema-constrained internal gateway client."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from reviewer.orchestrator.budget import BudgetExhausted
from reviewer.telemetry.activity import activity, llm_attempt, log, record, sink


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        stage: str,
        tier: str,
        system: str,
        user: str,
        response_model: type[BaseModel],
        review_id: UUID,
        max_tokens: int | None = None,
        timeout_s: float,
    ) -> BaseModel: ...


class FakeLLMClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    @activity("tool", "LLM gateway")
    async def complete(self, *, response_model, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response_model.model_validate(response)


class StageFailed(RuntimeError):
    pass


class GatewayClient:
    def __init__(
        self,
        settings,
        audit,
        budget,
        redactor,
        transport=None,
        models=None,
        semaphore=None,
    ):
        from urllib.parse import urlsplit

        import httpx

        from reviewer.config.models import resolve

        url = urlsplit(settings.gateway_base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username:
            raise ValueError("Internal gateway URL must be configured")
        self.client = httpx.AsyncClient(
            base_url=settings.gateway_base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {settings.gateway_key.get_secret_value()}",
                settings.classification_header: settings.classification,
            },
            timeout=60,
            follow_redirects=False,
            transport=transport,
        )
        # One resolved model per role, fixed for the life of this client so a
        # configuration edit cannot move a review onto a different model
        # halfway through it.
        self.specs = models if models is not None else resolve(settings)
        self.models = {role: spec.model for role, spec in self.specs.items()}
        self.audit, self.budget, self.redactor = audit, budget, redactor
        self.prices = settings.model_prices
        self.semaphore = (
            semaphore
            if semaphore is not None
            else asyncio.Semaphore(settings.gateway_concurrency)
        )

    @asynccontextmanager
    async def slot(self):
        started = monotonic()
        acquired = False
        outcome = "completed"
        try:
            try:
                remaining = (
                    self.budget.deadline_at - datetime.now(UTC)
                ).total_seconds()
                if remaining <= 0:
                    raise BudgetExhausted(
                        "Review deadline exhausted waiting for gateway capacity"
                    )
                try:
                    async with asyncio.timeout(remaining):
                        await self.semaphore.acquire()
                        acquired = True
                except TimeoutError:
                    raise BudgetExhausted(
                        "Review deadline exhausted waiting for gateway capacity"
                    ) from None
            except BaseException as exc:
                outcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
                raise
            finally:
                await record(
                    "wait",
                    dict(
                        name="Gateway capacity",
                        status=outcome,
                        duration_ms=(monotonic() - started) * 1000,
                    ),
                )
            yield
        finally:
            # Includes cancellation during optional telemetry after acquisition.
            if acquired:
                self.semaphore.release()

    @activity("tool", "LLM gateway")
    async def complete(self, **kwargs):
        async with self.slot():
            return await self._complete(**kwargs)

    async def _complete(
        self,
        *,
        stage,
        tier,
        system,
        user,
        response_model,
        review_id,
        max_tokens=None,
        timeout_s,
        prompt_version="1.0.0",
    ):
        import asyncio
        import json
        import random
        import time

        import httpx
        from pydantic import ValidationError

        spec = self.specs.get(tier)
        if spec is None or not spec.model:
            raise StageFailed("Model is not configured")
        model = spec.model
        # Legacy max_tokens arguments and model output caps are accepted for
        # compatibility, but never constrain generation. The gateway/model owns
        # its output capacity. Reserve the context window where affordable and
        # account actual usage afterwards. Near the review ceiling, reserve the
        # remaining allowance; an uncapped in-flight response can exceed it, but
        # settlement prevents any further calls once the budget is spent.
        system = self.redactor.text(system)
        user = self.redactor.text(user)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for parse_attempt in range(2):
            prompt = json.dumps(messages, ensure_ascii=False)
            prompt_tokens = len(prompt.encode())
            if prompt_tokens >= spec.context_tokens:
                raise StageFailed("Prompt exceeds configured context window")
            for attempt in range(3):
                wait_start = time.monotonic()
                if sink.get() is not None:
                    log.info(
                        "budget_wait_started", review_id=str(review_id), stage=stage
                    )
                try:
                    reserve = await self.budget.reserve(
                        spec.context_tokens,
                        stage=stage,
                        minimum_tokens=prompt_tokens + 1,
                    )
                finally:
                    budget_wait_ms = (time.monotonic() - wait_start) * 1000
                    if sink.get() is not None:
                        log.info(
                            "budget_wait_finished",
                            review_id=str(review_id),
                            stage=stage,
                            duration_ms=budget_wait_ms,
                        )

                start = time.monotonic()
                response_text = ""
                outcome = "transport_error"
                finish_reason = None
                validation_failure = None
                transport_failure = None
                # Without valid usage, charge the full possible context rather
                # than inventing a low completion count from an omitted cap.
                used = spec.context_tokens
                tokens_in = prompt_tokens
                tokens_out = spec.context_tokens - prompt_tokens
                try:
                    timeout = min(
                        timeout_s,
                        max(
                            0.001,
                            (
                                self.budget.deadline_at
                                - __import__("datetime").datetime.now(
                                    __import__("datetime").UTC
                                )
                            ).total_seconds(),
                        ),
                    )
                    body = {
                        "model": model,
                        "messages": messages,
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": response_model.__name__,
                                "strict": True,
                                "schema": strict_schema(
                                    response_model.model_json_schema()
                                ),
                            },
                        },
                    }
                    if spec.reasoning_effort:
                        body["reasoning_effort"] = spec.reasoning_effort
                    try:
                        async with asyncio.timeout(timeout):
                            response = await self.client.post(
                                "chat/completions", json=body, timeout=timeout
                            )
                    except TimeoutError:
                        raise httpx.ReadTimeout("Gateway wall-clock deadline") from None
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise TypeError("Invalid response envelope")
                    choice = data["choices"][0]
                    if not isinstance(choice, dict):
                        raise TypeError("Invalid response envelope")
                    reason = choice.get("finish_reason")
                    finish_reason = (
                        reason
                        if reason
                        in (
                            "stop",
                            "length",
                            "content_filter",
                            "tool_calls",
                            "function_call",
                        )
                        else "unknown"
                    )
                    message = choice.get("message")
                    if not isinstance(message, dict) or not isinstance(
                        message.get("content"), str
                    ):
                        raise TypeError("Invalid response content")
                    response_text = message["content"]
                    usage = data.get("usage")
                    if not isinstance(usage, dict):
                        usage = {}
                    # Invalid or missing usage must not break mandatory audit or
                    # release an optimistic token reservation. Booleans aren't counts.
                    reported_in = usage.get("prompt_tokens")
                    reported_out = usage.get("completion_tokens")
                    if type(reported_in) is int and reported_in >= 0:
                        tokens_in = reported_in
                    if type(reported_out) is int and reported_out >= 0:
                        tokens_out = reported_out
                    used = tokens_in + tokens_out
                    value = response_model.model_validate_json(response_text)
                    if finish_reason == "length":
                        # Even parseable JSON cannot establish complete coverage
                        # when the gateway says generation was cut short.
                        raise ValueError("Gateway output truncated")
                    outcome = "success"
                    return value
                except (
                    ValidationError,
                    ValueError,
                    KeyError,
                    IndexError,
                    TypeError,
                ) as exc:
                    outcome = "invalid_output"
                    validation_failure = (
                        "output_truncated"
                        if finish_reason == "length"
                        else "invalid_json"
                        if isinstance(exc, ValidationError)
                        and any(e["type"] == "json_invalid" for e in exc.errors())
                        else "schema_validation"
                        if isinstance(exc, ValidationError)
                        else "response_envelope"
                    )
                    if parse_attempt:
                        raise StageFailed("Structured output invalid") from None
                    break
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    transport_failure = (
                        "timeout"
                        if isinstance(exc, httpx.TimeoutException)
                        else "http_status"
                        if isinstance(exc, httpx.HTTPStatusError)
                        else "transport"
                    )
                    if attempt == 2:
                        raise StageFailed("Gateway transport failed") from None
                    await asyncio.sleep(0.1 * 2**attempt + random.random() * 0.05)
                finally:
                    await self.budget.settle(reserve, used)
                    # Unknown pricing stays None the whole way through: a model
                    # this installation has no price for did not cost nothing.
                    cost = (
                        (
                            tokens_in * self.prices[model].get("input", 0)
                            + tokens_out * self.prices[model].get("output", 0)
                        )
                        / 1_000_000
                        if model in self.prices
                        else None
                    )
                    latency_ms = int((time.monotonic() - start) * 1000)
                    await self.audit.write(
                        review_id=str(review_id),
                        stage=stage,
                        model=model,
                        prompt_version=prompt_version,
                        prompt=prompt,
                        response=self.redactor.text(response_text),
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        latency_ms=latency_ms,
                        cost=cost,
                        outcome=outcome,
                    )
                    await llm_attempt(
                        duration_ms=latency_ms,
                        budget_wait_ms=budget_wait_ms,
                        stage=stage,
                        role=tier,
                        model=model,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost=cost,
                        parse_attempt=parse_attempt + 1,
                        transport_attempt=attempt + 1,
                        outcome=outcome,
                        finish_reason=finish_reason,
                        validation_failure=validation_failure,
                        transport_failure=transport_failure,
                    )
            messages = [
                {
                    "role": "system",
                    "content": system
                    + (
                        " Your previous response reached the gateway output limit. Return a concise valid schema object."
                        if finish_reason == "length"
                        else " Your previous response failed schema validation. Return only a valid schema object."
                    ),
                },
                {"role": "user", "content": user},
            ]
        raise StageFailed("Stage failed")

    async def close(self):
        await self.client.aclose()


def strict_schema(schema):
    if isinstance(schema, dict):
        result = {k: strict_schema(v) for k, v in schema.items() if k != "default"}
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    return schema
