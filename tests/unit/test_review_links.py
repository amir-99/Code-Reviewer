import httpx

from reviewer.api.review_links import review_links
from reviewer.config.schema import Settings
from reviewer.services.forge.gitlab import GitLab


def settings():
    return Settings(
        _env_file=None,
        gitlab_base_url="https://git.example/code",
        jira_base_url="https://jira.example/jira",
        confluence_base_url="https://wiki.example/wiki",
    )


def test_reference_links_preserve_context_and_deduplicate():
    snapshot = {
        "bundle": {
            "mr": {"web_url": "https://git.example/code/group/repo/-/merge_requests/4"},
            "issue": {"key": "APP-1"},
            "epic": {"key": "APP-2"},
            "linkage": {"secondary_keys": ["APP-1", "APP-3"]},
            "documents": [
                {"title": "Design", "url": "https://wiki.example/wiki/pages/5"}
            ],
        }
    }
    links = review_links(
        snapshot,
        {
            "document_urls": [
                "https://wiki.example/wiki/pages/5",
                "https://wiki.example/pages/6",
            ]
        },
        settings(),
    )
    assert [link["kind"] for link in links] == [
        "gitlab",
        "jira",
        "jira",
        "jira",
        "confluence",
        "confluence",
    ]
    assert links[1]["url"] == "https://jira.example/jira/browse/APP-1"
    assert links[4]["label"] == "Design"


def test_reference_links_reject_unsafe_urls_and_use_overrides():
    snapshot = {
        "bundle": {
            "issue": {"key": "OLD-1"},
            "documents": [
                {"url": "javascript:alert(1)"},
                {"url": "https://evil.example/page"},
                {"url": "https://user:password@wiki.example/page"},
            ],
        }
    }
    links = review_links(
        snapshot,
        {"issue_key": "APP-4", "epic_key": "../../evil"},
        settings(),
        mr={"web_url": "https://git.example/other/group/repo/-/merge_requests/4"},
    )
    assert links == [
        {
            "kind": "jira",
            "label": "Jira issue: APP-4",
            "url": "https://jira.example/jira/browse/APP-4",
        }
    ]
    assert review_links({}, {}, settings()) == []


async def test_gitlab_keeps_web_url_for_review_navigation():
    url = "https://git.example/code/team/repo/-/merge_requests/4"
    forge = GitLab(
        "https://git.example/code",
        "test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"sha": "a" * 40, "web_url": url})
        ),
    )
    assert (await forge.get_merge_request(7, 4)).web_url == url
    await forge.close()
