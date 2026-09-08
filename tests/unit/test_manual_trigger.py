import httpx
import pytest
from conftest import FakeQueue

from reviewer.api.manual import parse_merge_request_url
from reviewer.config.schema import ProjectConfig, Settings
from reviewer.context.models import ReviewOverrides
from reviewer.context.requirements import collect
from reviewer.main import create_app
from reviewer.services.docs.confluence import DocumentContext, FakeDocumentService
from reviewer.services.forge.gitlab import MergeRequestContext
from reviewer.services.issues.jira import FakeIssueService, IssueContext

BASE = "https://gitlab.example.invalid"
WIKI = "https://wiki.example.invalid"


@pytest.mark.parametrize(
    "url,expected",
    [
        (f"{BASE}/group/proj/-/merge_requests/42", ("group/proj", 42)),
        (f"{BASE}/group/sub/proj/-/merge_requests/7", ("group/sub/proj", 7)),
        (f"{BASE}/group/proj/-/merge_requests/7/diffs", ("group/proj", 7)),
        (f"{BASE}/group/proj/-/merge_requests/7?tab=x#note_1", ("group/proj", 7)),
        (f"{BASE}/group/proj/merge_requests/7", ("group/proj", 7)),
    ],
)
def test_url_parsing(url, expected):
    assert parse_merge_request_url(url, BASE) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/group/proj/-/merge_requests/1",
        "http://gitlab.example.invalid/group/proj/-/merge_requests/1",
        "file:///etc/passwd",
        f"{BASE}/group/proj/-/issues/1",
        f"{BASE}/-/merge_requests/1",
        f"{BASE}/group/proj/-/merge_requests/notanumber",
    ],
)
def test_url_parsing_rejects_foreign_and_malformed(url):
    with pytest.raises(ValueError):
        parse_merge_request_url(url, BASE)


def test_url_parsing_honours_a_base_path_prefix():
    base = "https://host.invalid/gitlab"
    assert parse_merge_request_url(f"{base}/group/proj/-/merge_requests/3", base) == (
        "group/proj",
        3,
    )
    with pytest.raises(ValueError):
        parse_merge_request_url(
            "https://host.invalid/other/group/proj/-/merge_requests/3", base
        )


@pytest.fixture
def client_parts(store, forge):
    forge.projects["group/proj"] = 7
    queue = FakeQueue()
    app = create_app(
        Settings(
            project_ids=[7],
            webhook_secrets={},
            admin_token="admin",
            gitlab_base_url=BASE,
            # Pinned, not inherited: Settings reads .env, and a developer's real
            # Confluence host would otherwise decide whether these cases pass.
            confluence_base_url=WIKI,
            milestone="M0",
        ),
        store,
        queue,
        forge,
    )
    return app, queue, forge


async def post(app, body, token="admin"):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        return await client.post(
            "/admin/reviews",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )


async def test_manual_trigger_enqueues_job_with_overrides(client_parts):
    app, queue, _ = client_parts
    response = await post(
        app,
        {
            "merge_request_url": f"{BASE}/group/proj/-/merge_requests/2",
            "issue_key": "ABC-123",
            "epic_key": "ABC-100",
            "document_urls": [f"{WIKI}/x", f"{WIKI}/x"],
        },
    )
    assert response.status_code == 202
    assert response.json()["project_id"] == 7
    (name, args) = queue.jobs[0]
    assert name == "receive_event"
    assert args[0]["project_id"] == 7 and args[0]["iid"] == 2
    overrides = args[0]["overrides"]
    assert overrides["issue_key"] == "ABC-123"
    assert overrides["epic_key"] == "ABC-100"
    # Duplicate links are collapsed before they reach the budget.
    assert overrides["document_urls"] == [f"{WIKI}/x"]


async def test_manual_trigger_without_optional_context(client_parts):
    app, queue, _ = client_parts
    response = await post(
        app, {"merge_request_url": f"{BASE}/group/proj/-/merge_requests/2"}
    )
    assert response.status_code == 202
    overrides = queue.jobs[0][1][0]["overrides"]
    assert overrides["issue_key"] is None and overrides["document_urls"] == []


async def test_manual_trigger_rejects_bad_input(client_parts):
    app, queue, forge = client_parts
    assert (
        await post(app, {"merge_request_url": f"{BASE}/x/-/merge_requests/2"}, "no")
    ).status_code == 401
    assert (
        await post(
            app, {"merge_request_url": "https://evil.invalid/a/-/merge_requests/1"}
        )
    ).status_code == 400
    assert (
        await post(
            app,
            {
                "merge_request_url": f"{BASE}/group/proj/-/merge_requests/2",
                "issue_key": "not a key",
            },
        )
    ).status_code == 422
    assert (
        await post(
            app,
            {
                "merge_request_url": f"{BASE}/group/proj/-/merge_requests/2",
                "unexpected": 1,
            },
        )
    ).status_code == 422
    # A page on another host would be silently dropped at fetch time; say so.
    assert (
        await post(
            app,
            {
                "merge_request_url": f"{BASE}/group/proj/-/merge_requests/2",
                "document_urls": ["https://elsewhere.invalid/x"],
            },
        )
    ).status_code == 400
    # An unknown project path never reaches the queue.
    assert (
        await post(app, {"merge_request_url": f"{BASE}/other/proj/-/merge_requests/2"})
    ).status_code == 404
    forge.mr = MergeRequestContext(project_id=7, iid=2, head_sha="a" * 40, draft=True)
    assert (
        await post(app, {"merge_request_url": f"{BASE}/group/proj/-/merge_requests/2"})
    ).status_code == 409
    assert queue.jobs == []


def issues_service():
    return FakeIssueService(
        issues={
            "ABC-123": IssueContext(
                key="ABC-123",
                type="Story",
                summary="Story",
                description_md="body",
                status="Open",
            ),
            "ABC-100": IssueContext(
                key="ABC-100",
                type="Epic",
                summary="The epic",
                description_md="epic body",
                status="Open",
            ),
        },
        epics={},
        links={},
    )


def page(url):
    return DocumentContext(
        page_id="1",
        space="ENG",
        url=url,
        title="Spec",
        text_md="# Spec\nrules",
        version=1,
    )


async def test_collect_uses_manual_story_epic_and_pages():
    mr = MergeRequestContext(project_id=7, iid=2, head_sha="a" * 40)
    docs = FakeDocumentService({"https://wiki/x": page("https://wiki/x")})
    linkage, issue, epic, pages, degradations = await collect(
        mr,
        ProjectConfig(),
        issues_service(),
        docs,
        (),
        ReviewOverrides(
            issue_key="ABC-123",
            epic_key="ABC-100",
            document_urls=["https://wiki/x"],
        ),
    )
    assert linkage.issue_key == "ABC-123" and linkage.resolved_from == "manual"
    assert issue.key == "ABC-123"
    assert epic.key == "ABC-100" and epic.summary == "The epic"
    assert [p.url for p in pages] == ["https://wiki/x"]
    assert "unlinked" not in degradations


async def test_manual_pages_survive_an_unlinked_change():
    """Confluence links alone are enough; no Jira story is required."""
    mr = MergeRequestContext(project_id=7, iid=2, head_sha="a" * 40)
    docs = FakeDocumentService({"https://wiki/x": page("https://wiki/x")})
    linkage, issue, epic, pages, degradations = await collect(
        mr,
        ProjectConfig(),
        FakeIssueService(),
        docs,
        (),
        ReviewOverrides(document_urls=["https://wiki/x"]),
    )
    assert issue is None and "unlinked" in degradations
    assert [p.url for p in pages] == ["https://wiki/x"]


async def test_collect_without_overrides_is_unchanged():
    mr = MergeRequestContext(
        project_id=7, iid=2, head_sha="a" * 40, source_branch="feat/ABC-123"
    )
    linkage, issue, epic, pages, degradations = await collect(
        mr,
        ProjectConfig(issue_tracker={"project_keys": ["ABC"]}),
        issues_service(),
        FakeDocumentService(),
    )
    assert linkage.issue_key == "ABC-123" and linkage.resolved_from == "branch"
    assert epic is None and pages == []


async def test_manual_trigger_carries_report_mode(client_parts):
    app, queue, _ = client_parts
    url = f"{BASE}/group/proj/-/merge_requests/2"
    # Posting the report is the default a webhook run would also take.
    response = await post(app, {"merge_request_url": url})
    assert response.status_code == 202
    assert response.json()["report_mode"] == "applied"
    assert queue.jobs[0][1][0]["overrides"]["report_mode"] == "applied"
    # The trigger answers before the worker has run, so it cannot carry results.
    assert response.json()["findings"] is None
    for mode in ("draft", "none"):
        response = await post(app, {"merge_request_url": url, "report_mode": mode})
        assert response.status_code == 202
        assert response.json()["report_mode"] == mode
        assert queue.jobs[-1][1][0]["overrides"]["report_mode"] == mode
    assert (
        await post(app, {"merge_request_url": url, "report_mode": "publish"})
    ).status_code == 422
