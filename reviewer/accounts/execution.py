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


class PersonalDocs:
    """A Confluence client bound to one owner, one page and one page version.

    Same discipline as `PersonalForge`: every call re-checks the pinned
    credential, every write re-checks the publishing identity and that the page
    has not moved on since it was reviewed, and edits or deletions touch only
    comments this owner's reviewer posted.
    """

    WRITES = {"post_comment", "post_inline_comment", "edit_comment", "delete_comment"}

    def __init__(self, docs, guard, principal_id, page_id, version):
        self.docs, self.guard, self.principal_id = docs, guard, principal_id
        self.page_id, self.version = str(page_id), int(version)
        self.owner_user_id = None

    def marker(self):
        return f"<!-- ai-review:owner={self.owner_user_id} -->"

    def __getattr__(self, name):
        method = getattr(self.docs, name)
        if not callable(method):
            return method

        async def call(*args, **kwargs):
            await self.guard()
            if name in self.WRITES:
                if str(await self.docs.identity()) != self.principal_id:
                    raise CredentialUnavailable("Publishing identity changed")
                from reviewer.services.docs.confluence import DocumentRef

                page = await self.docs.fetch(
                    DocumentRef(
                        page_id=self.page_id, url=self.docs.page_url(self.page_id)
                    )
                )
                if page is None or int(page.version) != self.version:
                    raise StaleReview()
                if name in {"post_comment", "post_inline_comment"}:
                    if str(args[0]) != self.page_id:
                        raise CredentialUnavailable("Comment target changed")
                    args = list(args)
                    args[1] += "\n" + self.marker()
                else:
                    owned = [
                        c
                        for c in await self.docs.comments(self.page_id)
                        if c.id == str(args[0])
                        and c.author == self.principal_id
                        and self.marker() in c.body_storage
                    ]
                    if not owned:
                        raise CredentialUnavailable("Comment ownership changed")
                    if name == "edit_comment":
                        args = list(args)
                        args[1] += "\n" + self.marker()
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


@asynccontextmanager
async def personal_document_machine(ctx, review):
    """A document pipeline bound to the owner's Confluence and gateway tokens."""
    from reviewer.orchestrator.document_pipeline import DocumentPipeline
    from reviewer.services.docs.confluence import Confluence
    from reviewer.services.secrets.scanner import SecretScanner

    settings = ctx.get("settings") or ctx["machine"].settings
    service = Credentials(ctx["store"], settings)
    context = await service.load(review.owner_user_id, review.credential_refs or {})
    tokens = context.tokens
    if not tokens.get("confluence") or not tokens.get("gateway"):
        raise CredentialUnavailable("Confluence and gateway credentials are required")

    async def guard():
        await service.check_access(context)

    guard_token = credential_guard.set(guard)
    secret_token = personal_secrets.set(tuple(tokens.values()))
    docs = None
    try:
        docs = (ctx.get("personal_docs_factory") or Confluence)(
            settings.confluence_base_url, tokens["confluence"]
        )
        if hasattr(docs, "client"):
            docs.client.event_hooks.setdefault("request", []).append(check_request)
        if str(await docs.identity()) != review.principal_id:
            raise CredentialUnavailable("Publishing identity changed")
        subject = review.subject or {}
        personal_docs = PersonalDocs(
            docs,
            guard,
            review.principal_id,
            subject.get("page_id"),
            subject.get("version", 0),
        )
        personal_docs.owner_user_id = review.owner_user_id
        personal = settings.model_copy(
            update={
                "gateway_key": SecretStr(tokens["gateway"]),
                "confluence_token": SecretStr(tokens["confluence"]),
            }
        )
        machine = DocumentPipeline(
            ctx["store"],
            personal_docs,
            personal,
            ctx.get("document_scanner") or SecretScanner(),
            llm_factory=getattr(ctx.get("machine"), "llm_factory", None),
        )
        machine.gateway_slots = ctx.setdefault(
            "gateway_slots",
            getattr(
                ctx.get("machine"),
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
            if docs is not None and hasattr(docs, "close"):
                await docs.close()
        finally:
            personal_secrets.reset(secret_token)
            credential_guard.reset(guard_token)
