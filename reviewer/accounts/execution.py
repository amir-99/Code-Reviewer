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
        "edit_note",
        "delete_note",
        "edit_draft_note",
        "delete_draft_note",
        "publish_draft_note",
    }

    def __init__(self, forge, guard, principal_id, project, iid, head):
        self.forge, self.guard, self.principal_id = forge, guard, principal_id
        self.project, self.iid, self.head = project, iid, head
        self.owner_user_id = None

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
                        and f"<!-- ai-review:owner={self.owner_user_id} -->"
                        in d.notes[0].body
                        for d in discussions
                    ):
                        raise CredentialUnavailable("Discussion ownership changed")
                if name in {"edit_note", "delete_note"}:
                    notes = [
                        n
                        for d in await self.forge.list_discussions(
                            self.project, self.iid
                        )
                        for n in d.notes
                    ]
                    if not any(
                        n.id == args[2]
                        and str(n.author_id) == self.principal_id
                        and f"<!-- ai-review:owner={self.owner_user_id} -->" in n.body
                        for n in notes
                    ):
                        raise CredentialUnavailable("Comment ownership changed")
                if name in {
                    "edit_draft_note",
                    "delete_draft_note",
                    "publish_draft_note",
                }:
                    notes = await self.forge.list_draft_notes(self.project, self.iid)
                    if not any(
                        n.id == args[2]
                        and str(n.author_id) in {"0", self.principal_id}
                        and f"<!-- ai-review:owner={self.owner_user_id} -->" in n.body
                        for n in notes
                    ):
                        raise CredentialUnavailable("Draft ownership changed")
                await self.guard()
            if name in {
                "post_note",
                "post_inline_discussion",
                "post_draft_note",
                "reply",
            }:
                marker = f"\n\n<!-- ai-review:owner={self.owner_user_id} -->"
                if "body" in kwargs:
                    kwargs["body"] += marker
                else:
                    args = list(args)
                    args[3 if name == "reply" else 2] += marker
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
        personal_forge = PersonalForge(
            forge, guard, review.principal_id, project, review.mr_iid, head
        )
        personal_forge.owner_user_id = review.owner_user_id
        if settings.milestone == "M0":
            from reviewer.orchestrator.machine import ReviewStateMachine

            yield ReviewStateMachine(ctx["store"], personal_forge)
            return
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
            personal_forge,
            personal,
            GitService(
                personal.repo_cache, tokens["gitlab"], quota_root=settings.repo_cache
            ),
            issues,
            docs,
            SecretScanner(),
            StaticRunner(settings.static_enabled),
        )
        machine.forge.owner_user_id = review.owner_user_id
        machine.gateway_slots = ctx.setdefault(
            "gateway_slots",
            getattr(
                ctx["machine"],
                "gateway_slots",
                asyncio.Semaphore(settings.gateway_concurrency),
            ),
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
