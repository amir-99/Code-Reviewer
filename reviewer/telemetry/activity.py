"""Metadata-only activity tracing, scoped to one pipeline invocation."""

import asyncio
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

sink = ContextVar("review_activity_sink", default=None)
parent = ContextVar("review_activity_parent", default=None)


async def emit(kind, name, status, activity_id, parent_id):
    target = sink.get()
    if target is not None:
        store, review_id = target
        # Observability failures must not change the review decision.
        try:
            await store.append_event(
                review_id,
                kind,
                dict(
                    name=name,
                    status=status,
                    activity_id=activity_id,
                    parent_id=parent_id,
                ),
            )
        except Exception:
            pass


def activity(kind, name):
    """Only caller-defined labels are emitted; arguments/results are never logged."""

    def decorate(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            activity_id = str(uuid4())
            parent_id = parent.get()
            label = name(*args, **kwargs) if callable(name) else name
            await emit(kind, label, "started", activity_id, parent_id)
            token = parent.set(activity_id)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as exc:
                await emit(
                    kind,
                    label,
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "failed",
                    activity_id,
                    parent_id,
                )
                raise
            else:
                status = "partial" if getattr(result, "partial", False) else "completed"
                await emit(kind, label, status, activity_id, parent_id)
                return result
            finally:
                parent.reset(token)

        return wrapped

    return decorate


def traced_review(fn):
    @wraps(fn)
    async def wrapped(self, review_id):
        token = sink.set((self.store, review_id))
        try:
            return await fn(self, review_id)
        finally:
            sink.reset(token)

    return wrapped


async def llm_attempt(**metadata):
    """Persist caller-normalized attempt diagnostics without model content."""
    target = sink.get()
    if target is not None:
        store, review_id = target
        try:
            await store.append_event(
                review_id,
                "llm_attempt",
                {"parent_id": parent.get(), **metadata},
            )
        except Exception:
            pass
