"""Personal clients live for one pinned run and never reuse system credentials."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from pydantic import SecretStr

from reviewer.accounts.credentials import Credentials, CredentialUnavailable
from reviewer.services.forge.gitlab import GitLab, StaleReview

credential_guard = ContextVar("credential_guard", default=None)
personal_secrets = ContextVar("personal_secrets", default=())


async def check_request(request):
    guard = credential_guard.get()
    if guard:
        await guard()


class PersonalForge:
    WRITES = {
        "post_note",
        "post_inline_discussion",
        "post_draft_note",
        "reply",
        "resolve_discussion",
        "set_commit_status",
    }

    def __init__(self, forge, guard, principal_id, project, iid, head):
        self.forge, self.guard, self.principal_id = forge, guard, principal_id
        self.project, self.iid, self.head = project, iid, head

    def __getattr__(self, name):
        method = getattr(self.forge, name)
        if not callable(method):
            return method

        async def call(*args, **kwargs):
            await self.guard()
            if name in self.WRITES:
                # Re-fetch identity; a cached identity is not proof of author access.
                if hasattr(self.forge, "bot_id") and isinstance(self.forge, GitLab):
                    del self.forge.bot_id
                if str(await self.forge.identity()) != self.principal_id:
                    raise CredentialUnavailable("Publishing identity changed")
                mr = await self.forge.get_merge_request(self.project, self.iid)
                if mr.head_sha != self.head or mr.state != "opened" or mr.draft:
                    raise StaleReview()
                if name in {"reply", "resolve_discussion"}:
                    discussion_id = args[2]
                    discussions = await self.forge.list_discussions(
                        self.project, self.iid
                    )
                    if not any(
                        d.id == discussion_id
                        and d.notes
                        and str(d.notes[0].author_id) == self.principal_id
                        for d in discussions
                    ):
                        raise CredentialUnavailable("Discussion ownership changed")
                await self.guard()
            return await method(*args, **kwargs)

        return call


@asynccontextmanager
async def personal_machine(ctx, review, *, recheck=False):
    from reviewer.orchestrator.pipeline import Pipeline
    from reviewer.services.docs.confluence import Confluence, FakeDocumentService
    from reviewer.services.git.service import GitService
    from reviewer.services.issues.jira import FakeIssueService, Jira
    from reviewer.services.secrets.scanner import SecretScanner
    from reviewer.services.static.runner import StaticRunner

    settings = ctx.get("settings") or ctx["machine"].settings
    service = Credentials(ctx["store"], settings)
    context = await service.load(review.owner_user_id, review.credential_refs or {})
    tokens = context.tokens

    async def guard():
        await service.check_access(context)

    guard_token = credential_guard.set(guard)
    secret_token = personal_secrets.set(tuple(tokens.values()))
    clients = []
    try:
        forge = (ctx.get("personal_forge_factory") or GitLab)(
            settings.gitlab_base_url, tokens["gitlab"]
        )
        clients.append(forge)
        if str(await forge.identity()) != review.principal_id:
            raise CredentialUnavailable("Publishing identity changed")
        project = await ctx["store"].project_number(review)
        head = (
            (await forge.get_merge_request(project, review.mr_iid)).head_sha
            if recheck
            else review.head_sha
        )
        personal = settings.model_copy(
            update={
                "gitlab_token": SecretStr(tokens["gitlab"]),
                "git_read_token": SecretStr(tokens["gitlab"]),
                "gateway_key": SecretStr(tokens["gateway"]),
                "jira_token": SecretStr(tokens.get("jira", "")),
                "confluence_token": SecretStr(tokens.get("confluence", "")),
                "repo_cache": settings.repo_cache
                / "users"
                / review.owner_user_id
                / review.principal_id,
            }
        )
        issues = (
            Jira(settings.jira_base_url, tokens["jira"])
            if tokens.get("jira") and settings.jira_base_url
            else FakeIssueService()
        )
        docs = (
            Confluence(settings.confluence_base_url, tokens["confluence"])
            if tokens.get("confluence") and settings.confluence_base_url
            else FakeDocumentService()
        )
        clients.extend([issues, docs])
        for client in clients:
            if hasattr(client, "client"):
                client.client.event_hooks.setdefault("request", []).append(
                    check_request
                )
        machine = Pipeline(
            ctx["store"],
            PersonalForge(
                forge, guard, review.principal_id, project, review.mr_iid, head
            ),
            personal,
            GitService(personal.repo_cache, tokens["gitlab"]),
            issues,
            docs,
            SecretScanner(),
            StaticRunner(settings.static_enabled),
        )
        machine.forge.owner_user_id = review.owner_user_id
        machine.gateway_slots = ctx.setdefault(
            "gateway_slots", asyncio.Semaphore(settings.gateway_concurrency)
        )
        machine.owner_user_id, machine.principal_id = (
            review.owner_user_id,
            review.principal_id,
        )
        yield machine
    finally:
        try:
            for client in clients:
                if hasattr(client, "close"):
                    await client.close()
        finally:
            personal_secrets.reset(secret_token)
            credential_guard.reset(guard_token)
