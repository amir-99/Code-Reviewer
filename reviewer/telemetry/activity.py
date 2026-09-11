"""Metadata-only activity tracing, scoped to one pipeline invocation."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from time import monotonic
from uuid import uuid4

import structlog

sink = ContextVar("review_activity_sink", default=None)
parent = ContextVar("review_activity_parent", default=None)


# Activity rows are optional; mandatory stage and LLM audit persistence is separate.
EVENT_TIMEOUT_S = 1.0
log = structlog.get_logger()


@contextmanager
def storage_timing(name, review_id):
    """Time database phases without recursive event writes or cancellation points."""
    started = monotonic()
    status = "completed"
    log.info("storage_started", name=name, review_id=str(review_id))
    try:
        yield
    except BaseException:
        status = "failed"
        raise
    finally:
        log.info(
            "storage_finished",
            name=name,
            review_id=str(review_id),
            status=status,
            duration_ms=(monotonic() - started) * 1000,
        )


async def record(kind, data):
    target = sink.get()
    if target is None:
        return
    store, review_id = target
    started = monotonic()
    status = "completed"
    # Emit before touching the database so an outstanding write is visible.
    log.info("activity_event", review_id=str(review_id), kind=kind, **data)
    try:
        async with asyncio.timeout(EVENT_TIMEOUT_S):
            await store.append_event(review_id, kind, data)
    except Exception as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "failed"
        log.warning(
            "activity_write_failed",
            review_id=str(review_id),
            kind=kind,
            error_type=type(exc).__name__,
        )
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        log.info(
            "activity_write_finished",
            review_id=str(review_id),
            kind=kind,
            status=status,
            duration_ms=round((monotonic() - started) * 1000, 3),
        )


async def emit(kind, name, status, activity_id, parent_id, **timing):
    await record(
        kind,
        dict(
            name=name,
            status=status,
            activity_id=activity_id,
            parent_id=parent_id,
            **timing,
        ),
    )


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
            started = monotonic()
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
                    duration_ms=(monotonic() - started) * 1000,
                    error_type=type(exc).__name__,
                )
                raise
            else:
                status = "partial" if getattr(result, "partial", False) else "completed"
                await emit(
                    kind,
                    label,
                    status,
                    activity_id,
                    parent_id,
                    duration_ms=(monotonic() - started) * 1000,
                )
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
            return await activity("pipeline", "Review invocation")(fn)(self, review_id)
        finally:
            sink.reset(token)

    return wrapped


async def llm_attempt(**metadata):
    """Persist caller-normalized attempt diagnostics without model content."""
    await record("llm_attempt", {"parent_id": parent.get(), **metadata})


# The run bracket. A review reaches a terminal state before its worker is done:
# the commit status is delivered and the worktree torn down afterwards, and both
# still write activity. Bracketing the run lets a reader tell a review that has
# reached a terminal state from one whose run is actually over.
RUN = "run"
CLOSED = {
    "PUBLISHED": "completed",
    "TERMINATED_EARLY": "completed",
    "FAILED_CONTEXT": "failed",
    "FAILED_INTERNAL": "failed",
    "CANCELLED": "cancelled",
    "SUPERSEDED": "cancelled",
}


async def _run_event(store, review_id, status):
    # One durable row per run, opened and closed on the same activity id.
    try:
        await store.append_event(
            review_id,
            RUN,
            dict(
                name="review run",
                status=status,
                activity_id=f"{RUN}:{review_id}",
                parent_id=None,
            ),
        )
    except Exception:
        pass


async def open_run(store, review_id):
    """Record that a worker has taken this review's run."""
    await _run_event(store, review_id, "started")


async def close_run(store, review_id):
    """Record that the worker is finished with this review, terminal or not."""
    try:
        review = await store.get(review_id)
    except Exception:
        return
    state = getattr(review, "state", None)
    await _run_event(store, review_id, CLOSED.get(str(state), "failed"))
