import structlog
from arq import cron
from arq.connections import RedisSettings

from reviewer.api.webhooks import ReviewJob
from reviewer.config.schema import Settings
from reviewer.orchestrator.machine import ReviewStateMachine
from reviewer.services.forge.gitlab import GitLab
from reviewer.store.repositories import Store
from reviewer.telemetry import configure

logger = structlog.get_logger()


async def startup(ctx):
    settings = Settings()
    ctx["settings"] = settings
    configure(settings.log_level)
    from reviewer.telemetry import configure_traces

    configure_traces(settings.otlp_endpoint)
    logger.info(
        "worker_startup",
        milestone=settings.milestone,
        gitlab_url=settings.gitlab_base_url,
    )
    ctx["store"] = Store(settings.database_url.get_secret_value())
    ctx["forge"] = GitLab(
        settings.gitlab_base_url, settings.gitlab_token.get_secret_value()
    )
    # Keep existing webhook deployments onboarded; manual-only projects need
    # no webhook secret or registered hook.
    await ctx["store"].provision(
        set(settings.project_ids) | set(settings.webhook_secrets)
    )
    if settings.milestone == "M0":
        ctx["machine"] = ReviewStateMachine(ctx["store"], ctx["forge"])
    else:
        from reviewer.orchestrator.pipeline import Pipeline
        from reviewer.services.docs.confluence import Confluence, FakeDocumentService
        from reviewer.services.git.service import GitService
        from reviewer.services.issues.jira import FakeIssueService, Jira
        from reviewer.services.secrets.scanner import SecretScanner
        from reviewer.services.static.runner import StaticRunner

        issues = (
            Jira(settings.jira_base_url, settings.jira_token.get_secret_value())
            if settings.jira_base_url
            else FakeIssueService()
        )
        docs = (
            Confluence(
                settings.confluence_base_url,
                settings.confluence_token.get_secret_value(),
            )
            if settings.confluence_base_url
            else FakeDocumentService()
        )
        ctx["issues"], ctx["docs"] = issues, docs
        from reviewer.store.audit import blob_store

        ctx["blobs"] = blob_store(settings)
        ctx["machine"] = Pipeline(
            ctx["store"],
            ctx["forge"],
            settings,
            GitService(settings.repo_cache, settings.git_read_token.get_secret_value()),
            issues,
            docs,
            SecretScanner(),
            StaticRunner(settings.static_enabled),
        )


async def shutdown(ctx):
    logger.info("worker_shutdown")
    for key in ("issues", "docs"):
        if key in ctx and hasattr(ctx[key], "close"):
            await ctx[key].close()
    await ctx["forge"].close()
    await ctx["store"].engine.dispose()


async def receive_event(ctx, payload):
    from reviewer.accounts.credentials import Credentials, CredentialUnavailable
    from reviewer.store.models import ReviewTrigger

    job = ReviewJob.model_validate(payload)
    async with ctx["store"].sessions() as session:
        trigger = await session.get(ReviewTrigger, job.event_id)
    if trigger is not None:
        if trigger.state != "QUEUED":
            return
        settings = ctx.get("settings") or ctx["machine"].settings
        try:
            context = await Credentials(ctx["store"], settings).load(
                trigger.owner_user_id, trigger.credential_refs
            )
            forge = (ctx.get("personal_forge_factory") or GitLab)(
                settings.gitlab_base_url, context.tokens["gitlab"]
            )
            try:
                if str(await forge.identity()) != trigger.payload["principal_id"]:
                    raise CredentialUnavailable("Publishing identity changed")
                return await _receive_event(
                    ctx | {"forge": forge, "personal_trigger": trigger}, trigger.payload
                )
            finally:
                await forge.close()
        except CredentialUnavailable:
            async with ctx["store"].transaction() as session:
                saved = await session.get(ReviewTrigger, job.event_id)
                saved.state = "REJECTED"
            return
    return await _receive_event(ctx, payload)


async def _receive_event(ctx, payload):
    job = ReviewJob.model_validate(payload)
    logger.info(
        "receive_event",
        action=job.action,
        project_id=job.project_id,
        iid=job.iid,
        event_id=job.event_id,
    )
    if job.action == "command":
        from reviewer.publish.commands import command

        return await command(
            ctx,
            job.project_id,
            job.iid,
            job.user_id,
            job.command,
            job.discussion_id,
            job.note_id,
        )
    if job.action == "pipeline":
        for review in await ctx["store"].pending():
            if (
                review.head_sha == job.head_sha
                and await ctx["store"].project_number(review) == job.project_id
            ):
                data = await ctx["store"].snapshot(review.id) or {}
                data["ci_status"] = job.ci_status
                await ctx["store"].save_snapshot(review.id, data)
        return
    if job.action == "cancel":
        logger.info("cancel_reviews", project_id=job.project_id, iid=job.iid)
        await ctx["store"].cancel(job.project_id, job.iid)
        return
    # Read current head before admission so delayed hooks cannot supersede a
    # newer run with an older SHA. The machine independently re-checks it.
    mr = await ctx["forge"].get_merge_request(job.project_id, job.iid)
    if mr.state != "opened" or mr.draft:
        if ctx.get("personal_trigger"):
            from reviewer.store.models import ReviewTrigger

            async with ctx["store"].transaction() as session:
                trigger = await session.get(ReviewTrigger, job.event_id)
                trigger.state = "REJECTED"
        logger.info(
            "mr_skipped",
            project_id=job.project_id,
            iid=job.iid,
            state=mr.state,
            draft=mr.draft,
        )
        return
    # The authenticated admin endpoint already resolved this project using our
    # GitLab token. Manual requests need no prior operator onboarding. Hook
    # payloads cannot set overrides, so they cannot provision projects here.
    trigger = ctx.get("personal_trigger")
    if trigger or (job.overrides and job.overrides.requested_by == "admin"):
        await ctx["store"].provision([job.project_id])
    review = await ctx["store"].accept(
        job.project_id,
        job.iid,
        mr.head_sha,
        job.event_id,
        job.overrides.model_dump() if job.overrides else None,
        owner_user_id=trigger.owner_user_id if trigger else None,
        credential_refs=trigger.credential_refs if trigger else None,
        principal_id=trigger.payload["principal_id"] if trigger else None,
        trigger_source="manual"
        if trigger
        else "command"
        if job.event_id.startswith("command:")
        else "system",
    )
    if review is None:
        return {"accepted": False, "reason": "conflict"}
    logger.info(
        "review_admitted",
        review_id=review.id,
        project_id=job.project_id,
        iid=job.iid,
        head_sha=mr.head_sha,
    )
    await ctx["redis"].enqueue_job("run_review", review.id, _job_id=f"run:{review.id}")


async def run_review(ctx, review_id):
    from opentelemetry import trace

    from reviewer.telemetry.activity import close_run, open_run

    review = await ctx["store"].get(review_id)
    if review is None:
        return
    logger.info("review_started", review_id=review_id)
    with trace.get_tracer(__name__).start_as_current_span(
        "review", attributes={"review.id": review_id}
    ):
        # A terminal state is committed before the run is over: the commit
        # status is delivered and the worktree torn down after it, and both
        # still write activity. The bracket is what tells a reader the run
        # itself has ended; it is best effort and never fails the review.
        await open_run(ctx["store"], review_id)
        try:
            project = await ctx["store"].project_number(review)
            async with ctx["store"].mr_lock(project, review.mr_iid):
                if review.owner_user_id:
                    from reviewer.accounts.credentials import CredentialUnavailable
                    from reviewer.accounts.execution import personal_machine

                    try:
                        async with personal_machine(ctx, review) as machine:
                            return await machine.run(review_id)
                    except CredentialUnavailable:
                        await ctx["store"].transition(
                            review_id,
                            "FAILED_CONTEXT",
                            partial=True,
                            error="Account or pinned credentials unavailable",
                        )
                        return
                return await ctx["machine"].run(review_id)
        finally:
            await close_run(ctx["store"], review_id)


async def recheck_review(ctx, project_id, iid, review_id=None):
    """Answer the open review comments on a merge request at its current head.

    Deliberately not a review: no admission, no supersession and no report, so
    an author asking "did my fixes land?" never cancels a run in flight.
    """
    machine = ctx["machine"]
    if not hasattr(machine, "recheck_now"):
        logger.info("recheck_unavailable", project_id=project_id, iid=iid)
        return {"rechecked": False, "reason": "milestone"}
    logger.info("recheck_requested", project_id=project_id, iid=iid)
    async with ctx["store"].mr_lock(project_id, iid):
        review = await ctx["store"].get(review_id) if review_id else None
        if review and review.owner_user_id:
            from reviewer.accounts.credentials import CredentialUnavailable
            from reviewer.accounts.execution import personal_machine

            try:
                async with personal_machine(ctx, review, recheck=True) as personal:
                    return await personal.recheck_now(
                        project_id, iid, review_id=review_id
                    )
            except CredentialUnavailable:
                return {"rechecked": False, "reason": "credentials_unavailable"}
        return await machine.recheck_now(project_id, iid, review_id=review_id)


async def replay_review(ctx, project_id, iid, event_id, overrides=None):
    mr = await ctx["forge"].get_merge_request(project_id, iid)
    logger.info(
        "replay_review",
        project_id=project_id,
        iid=iid,
        head_sha=mr.head_sha,
        event_id=event_id,
    )
    await receive_event(
        ctx,
        ReviewJob(
            project_id=project_id,
            iid=iid,
            head_sha=mr.head_sha,
            event_id=event_id,
            # Replaying a manual review keeps the context it was triggered with.
            overrides=overrides,
        ).model_dump(),
    )


async def recover(ctx):
    if "blobs" in ctx:
        ctx["blobs"].reap()
    from sqlalchemy import select

    from reviewer.store.models import ReviewTrigger

    async with ctx["store"].sessions() as session:
        triggers = (
            await session.scalars(
                select(ReviewTrigger).where(ReviewTrigger.state == "QUEUED")
            )
        ).all()
    for trigger in triggers:
        await ctx["redis"].enqueue_job(
            "receive_event", trigger.payload, _job_id=trigger.event_id
        )
    # Durable DB admission + sweep closes the DB-commit/Redis-enqueue crash gap.
    pending = await ctx["store"].pending()
    if pending:
        logger.info("recovering_reviews", count=len(pending))
    for review in pending:
        await ctx["redis"].enqueue_job(
            "run_review", review.id, _job_id=f"run:{review.id}"
        )


class WorkerSettings:
    functions = [receive_event, run_review, replay_review, recheck_review]
    cron_jobs = [cron(recover, second={0, 30}, run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(Settings().redis_url.get_secret_value())
    max_jobs = 10
    job_timeout = 1200
    keep_result = 0
    max_tries = 5
    health_check_interval = 30
