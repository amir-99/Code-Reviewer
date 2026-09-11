"""Audited, budgeted, schema-constrained internal gateway client."""

from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from reviewer.telemetry.activity import activity, llm_attempt, log, sink


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
        max_tokens: int,
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
    def __init__(self, settings, audit, budget, redactor, transport=None, models=None):
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

    @activity("tool", "LLM gateway")
    async def complete(
        self,
        *,
        stage,
        tier,
        system,
        user,
        response_model,
        review_id,
        max_tokens,
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
        # A stage may not ask for more output than its own model will return.
        if spec.max_output_tokens:
            max_tokens = min(max_tokens, spec.max_output_tokens)
        system = self.redactor.text(system)
        user = self.redactor.text(user)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        prompt = json.dumps(messages, ensure_ascii=False)
        reserve = (
            len(prompt.encode()) + max_tokens
        )  # conservative byte count upper bound
        if reserve > spec.context_tokens:
            raise StageFailed("Prompt exceeds configured context window")
        for parse_attempt in range(2):
            prompt = json.dumps(messages, ensure_ascii=False)
            reserve = len(prompt.encode()) + max_tokens
            for attempt in range(3):
                wait_start = time.monotonic()
                if sink.get() is not None:
                    log.info(
                        "budget_wait_started", review_id=str(review_id), stage=stage
                    )
                try:
                    await self.budget.reserve(reserve, stage=stage)
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
                used = reserve
                tokens_in = len(prompt.encode())
                tokens_out = max_tokens
                try:
                    timeout = min(
                        timeout_s,
                        max(
                            0.001,
                            (
                                self.budget.budget.deadline_at
                                - __import__("datetime").datetime.now(
                                    __import__("datetime").UTC
                                )
                            ).total_seconds(),
                        ),
                    )
                    body = {
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
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
                    response = await self.client.post(
                        "chat/completions", json=body, timeout=timeout
                    )
                    response.raise_for_status()
                    data = response.json()
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
                    response_text = choice["message"]["content"]
                    tokens_in = data.get("usage", {}).get(
                        "prompt_tokens", len(prompt.encode())
                    )
                    tokens_out = data.get("usage", {}).get(
                        "completion_tokens", len(response_text.encode())
                    )
                    used = tokens_in + tokens_out
                    value = response_model.model_validate_json(response_text)
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
                        "invalid_json"
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
                    + " Your previous response failed schema validation. Return only a valid schema object.",
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
