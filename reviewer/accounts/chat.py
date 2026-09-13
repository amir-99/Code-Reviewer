"""Answer one stored question with the review owner's own credentials.

A question is a job, not a review: no admission, no state transition, no lock
on the merge request. It reads the stored record and — only when the model asks
— code at the reviewed commit, an issue, or a page, each with the credential
the owner pinned when asking. A missing personal credential makes that source
unavailable; it never falls back to installation secrets.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from pydantic import SecretStr

from reviewer.accounts.credentials import Credentials, CredentialUnavailable
from reviewer.accounts.execution import (
    check_request,
    credential_guard,
    personal_secrets,
)
from reviewer.agents.chat import ChatFailed, answer
from reviewer.config.loader import load_project
from reviewer.config.models import assignment, resolve
from reviewer.config.schema import ProjectConfig
from reviewer.context.chat_context import ChatSources
from reviewer.context.models import Budget
from reviewer.context.redaction import Redactor
from reviewer.orchestrator.budget import BudgetExhausted, BudgetTracker
from reviewer.orchestrator.states import TERMINAL
from reviewer.services.llm.client import GatewayClient, StageFailed

logger = structlog.get_logger()

# A pending question older than this and still unanswered has lost its worker.
STALE_AFTER = timedelta(minutes=15)


async def answer_chat(ctx, message_id):
    store = ctx["store"]
    message = await store.chat_message(message_id)
    if message is None or message.status != "pending":
        return {"answered": False, "reason": "settled"}
    review = await store.get(message.review_id)
    snapshot = await store.snapshot(message.review_id)
    if (
        review is None
        or review.owner_user_id is None
        or review.owner_user_id != message.user_id
        or review.state not in TERMINAL
        or not snapshot
    ):
        await store.finish_chat_message(
            message_id, status="failed", error="Review is not available for chat"
        )
        return {"answered": False, "reason": "ineligible"}
    before = await store.chat_usage(review.id)
    try:
        result = await _answer(ctx, review, snapshot, message)
    except CredentialUnavailable:
        error = "Personal credentials are unavailable"
    except BudgetExhausted:
        error = "The chat budget for this review is spent"
    except (ChatFailed, StageFailed):
        error = "The model could not answer this question"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("chat_failed", message_id=message_id)
        error = "Answering failed"
    else:
        text, citations, used, model = result
        await store.finish_chat_message(
            message_id,
            status="answered",
            answer=text,
            citations=citations,
            context_used=used,
            model=model,
            **await _usage(store, review.id, before),
        )
        return {"answered": True}
    await store.finish_chat_message(
        message_id,
        status="failed",
        error=error,
        **await _usage(store, review.id, before),
    )
    return {"answered": False, "reason": error}


async def _usage(store, review_id, before):
    """What this question cost: the audited chat calls since it started."""
    after = await store.chat_usage(review_id)
    return {"tokens_in": after[0] - before[0], "tokens_out": after[1] - before[1]}


async def _answer(ctx, review, snapshot, message):
    from reviewer.services.docs.confluence import Confluence
    from reviewer.services.git.service import GitService
    from reviewer.services.issues.jira import Jira
    from reviewer.services.secrets.scanner import SecretScanner
    from reviewer.store.audit import Audit, blob_store

    store = ctx["store"]
    settings = ctx.get("settings") or ctx["machine"].settings
    project = await store.project_number(review)
    frozen = (review.execution_config or {}).get("config") or snapshot.get("config")
    config = (
        ProjectConfig.model_validate(frozen)
        if frozen
        else load_project(settings.config_path, project)
    )
    # The live project file decides whether chat is on; the frozen copy decides
    # everything else, so the answer describes the run as it was configured.
    if not load_project(settings.config_path, project).chat.enabled:
        raise ChatFailed("Chat is disabled for this project")
    service = Credentials(store, settings)
    context = await service.load(
        review.owner_user_id, message.credential_refs or {}, required=False
    )
    tokens = context.tokens
    if not tokens.get("gateway"):
        raise CredentialUnavailable("No personal gateway credential")

    async def guard():
        await service.check_access(context)

    guard_token = credential_guard.set(guard)
    secret_token = personal_secrets.set(tuple(tokens.values()))
    clients = []
    sources = None
    llm = None
    try:
        personal = settings.model_copy(
            update={
                "gateway_key": SecretStr(tokens["gateway"]),
                "gitlab_token": SecretStr(tokens.get("gitlab", "")),
                "git_read_token": SecretStr(tokens.get("gitlab", "")),
            }
        )
        issues = (
            Jira(settings.jira_base_url, tokens["jira"])
            if tokens.get("jira") and settings.jira_base_url
            else None
        )
        docs = (
            Confluence(settings.confluence_base_url, tokens["confluence"])
            if tokens.get("confluence") and settings.confluence_base_url
            else None
        )
        for client in (issues, docs):
            if client is not None:
                clients.append(client)
                client.client.event_hooks.setdefault("request", []).append(
                    check_request
                )
        git = (
            (ctx.get("chat_git_factory") or _git)(settings, review, tokens, GitService)
            if tokens.get("gitlab")
            else None
        )
        redactor = Redactor()
        sources = ChatSources(
            store,
            review,
            snapshot,
            config,
            git=git,
            repo_url=((snapshot.get("bundle") or {}).get("mr") or {}).get(
                "repository_url", ""
            ),
            issues=issues if issues else ctx.get("chat_issues"),
            docs=docs if docs else ctx.get("chat_docs"),
            scanner=ctx.get("chat_scanner") or SecretScanner(),
            redactor=redactor,
        )
        models = resolve(personal, config)
        spec = models.get("chat")
        if spec is None:
            raise StageFailed("Chat model is not configured")
        budget = BudgetTracker(
            Budget(
                token_ceiling=config.chat.token_ceiling,
                deadline_at=datetime.now(UTC)
                + timedelta(seconds=config.chat.timeout_s),
                model_tier=assignment(models),
                tokens_used=await store.chat_tokens(review.id),
            )
        )
        factory = getattr(ctx.get("machine"), "llm_factory", None)
        llm = (
            factory(None, redactor)
            if factory
            else GatewayClient(
                personal,
                Audit(store, blob_store(settings)),
                budget,
                redactor,
                models=models,
                semaphore=ctx.setdefault(
                    "gateway_slots",
                    getattr(
                        ctx.get("machine"),
                        "gateway_slots",
                        asyncio.Semaphore(settings.gateway_concurrency),
                    ),
                ),
            )
        )
        return await answer(sources, message.question, llm, config, review.id, spec)
    finally:
        try:
            if sources is not None:
                await sources.close()
            if llm is not None and hasattr(llm, "close"):
                await llm.close()
            for client in clients:
                await client.close()
        finally:
            personal_secrets.reset(secret_token)
            credential_guard.reset(guard_token)


def _git(settings, review, tokens, GitService):
    return GitService(
        settings.repo_cache / "users" / review.owner_user_id / review.principal_id,
        tokens["gitlab"],
        quota_root=settings.repo_cache,
    )


async def recover_chat(ctx):
    """Re-enqueue questions whose worker died; arq drops a duplicate job id."""
    for message_id in await ctx["store"].stale_chat_messages(
        datetime.now(UTC) - STALE_AFTER
    ):
        await ctx["redis"].enqueue_job(
            "answer_chat", message_id, _job_id=f"chat:{message_id}"
        )
