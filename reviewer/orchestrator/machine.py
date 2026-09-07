import structlog
from opentelemetry import trace

from reviewer.orchestrator.states import TERMINAL
from reviewer.telemetry import REVIEWS

log = structlog.get_logger()
tracer = trace.get_tracer(__name__)


class ReviewStateMachine:
    """M0 resumable no-op pipeline. No pending/failed status or comments emitted.

    State is committed before each step. Delivery failures escape to arq for
    retry; the durable status_delivered bit lets the sweeper recover them too.
    Cancellation by worker shutdown deliberately leaves the last state intact.
    """

    def __init__(self, store, forge):
        self.store, self.forge = store, forge

    async def run(self, review_id: str):
        with tracer.start_as_current_span(
            "review", attributes={"review.id": review_id}
        ):
            review = await self.store.get(review_id)
            if review is None:
                return
            project_id = await self.store.project_number(review)
            try:
                while review.state not in TERMINAL:
                    current = await self.forge.get_merge_request(
                        project_id, review.mr_iid
                    )
                    if current.head_sha != review.head_sha:
                        review = await self.store.transition(review_id, "SUPERSEDED")
                        # Persist replacement now; the sweeper enqueues it even if
                        # this process dies before returning to its queue handler.
                        await self.store.accept(
                            project_id,
                            review.mr_iid,
                            current.head_sha,
                            f"supersede:{review_id}:{current.head_sha}",
                        )
                        break
                    if current.state != "opened" or current.draft:
                        review = await self.store.transition(review_id, "CANCELLED")
                        break
                    if review.state == "INIT":
                        await self.store.noop(review_id)
                        review = await self.store.transition(review_id, "FINALIZATION")
                    elif review.state == "FINALIZATION":
                        review = await self.store.transition(
                            review_id, "DECISION", decision="COMMENT_ONLY"
                        )
                    elif review.state == "DECISION":
                        review = await self.store.transition(review_id, "PUBLISHED")
                    else:
                        raise ValueError("State requires an unavailable milestone")
            except Exception as exc:
                # Never log response bodies, request URLs or exception text: an
                # upstream failure may contain credentials or private payloads.
                log.error(
                    "review_failed", review_id=review_id, error_type=type(exc).__name__
                )
                review = await self.store.transition(
                    review_id,
                    "FAILED_INTERNAL",
                    decision="COMMENT_ONLY",
                    error=type(exc).__name__,
                )
            if not review.status_delivered:
                await self.forge.set_commit_status(
                    project_id,
                    review.head_sha,
                    "success",
                    "ai-review",
                    "M0 infrastructure check; AI analysis is not enabled",
                    "",
                )
                await self.store.mark_status(review_id)
            REVIEWS.labels(state=review.state).inc()
            return review.state
