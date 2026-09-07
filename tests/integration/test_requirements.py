import httpx

from reviewer.config.schema import ProjectConfig
from reviewer.context.requirements import collect
from reviewer.services.docs.confluence import Confluence, FakeDocumentService
from reviewer.services.forge.gitlab import MergeRequestContext
from reviewer.services.issues.jira import FakeIssueService, IssueContext, Jira


async def test_all_confluence_url_forms_and_redirect_boundary():
    def handler(req):
        if "/x/" in req.url.path:
            return httpx.Response(
                302, headers={"location": "/pages/viewpage.action?pageId=123"}
            )
        return httpx.Response(200, json={"results": [{"id": "123"}]})

    docs = Confluence(
        "https://wiki.internal", "token", transport=httpx.MockTransport(handler)
    )
    for path in [
        "/pages/viewpage.action?pageId=123",
        "/spaces/PAY/pages/123/slug",
        "/display/PAY/Hello+World",
        "/x/tiny",
    ]:
        assert (await docs.resolve("https://wiki.internal" + path)).page_id == "123"
    assert await docs.resolve("https://evil.invalid/x/tiny") is None
    await docs.close()


async def test_dynamic_epic_discovery_and_parent_fallback():
    for parent in [False, True]:

        def handler(req):
            if req.url.path.endswith("/field"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": "customfield_987",
                            "schema": {
                                "custom": "com.pyxis.greenhopper.jira:gh-epic-link"
                            },
                        }
                    ],
                )
            fields = {
                "summary": "Story",
                "description": "h2. Acceptance Criteria\n* Works",
                "issuetype": {"name": "Story"},
                "status": {"name": "Open"},
            }
            fields.update(
                {"parent": {"key": "PAY-1", "fields": {"issuetype": {"name": "Epic"}}}}
                if parent
                else {"customfield_987": "PAY-1"}
            )
            return httpx.Response(200, json={"fields": fields})

        jira = Jira(
            "https://jira.internal", "token", transport=httpx.MockTransport(handler)
        )
        issue = await jira.get_issue("PAY-2")
        assert issue.acceptance_criteria[0].text == "Works"
        assert (await jira.get_epic_for("PAY-2")).key == "PAY-1"
        await jira.close()


async def test_missing_key_missing_issue_and_docs_outage_degrade():
    mr = MergeRequestContext(
        project_id=1, iid=1, head_sha="a" * 40, source_branch="feature"
    )
    config = ProjectConfig(issue_tracker={"project_keys": ["PAY"]})
    args = await collect(mr, config, FakeIssueService(), FakeDocumentService())
    assert "unlinked" in args[-1]
    mr.source_branch = "PAY-2/story"
    args = await collect(mr, config, FakeIssueService(), FakeDocumentService())
    assert args[0].warnings
    issue = IssueContext(
        key="PAY-2", type="Story", summary="story", description_md="", status="Open"
    )

    class Broken(FakeDocumentService):
        async def resolve(self, url):
            raise TimeoutError()

    args = await collect(
        mr,
        config,
        FakeIssueService(
            issues={"PAY-2": issue}, links={"PAY-2": ["https://wiki.internal/x/a"]}
        ),
        Broken(),
    )
    assert "docs_unavailable" in args[-1] and args[1] is not None


async def test_confluence_redirect_cannot_send_token_to_external_host():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            302,
            headers={
                "location": "https://external.invalid/pages/viewpage.action?pageId=5"
            },
        )

    docs = Confluence(
        "https://wiki.internal", "private-token", transport=httpx.MockTransport(handler)
    )
    assert await docs.resolve("https://wiki.internal/x/example") is None
    assert len(calls) == 1 and calls[0].url.host == "wiki.internal"
    await docs.close()
