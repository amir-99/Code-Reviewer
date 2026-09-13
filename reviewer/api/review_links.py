"""Small, origin-checked navigation links from the review's recorded context."""

import re
from urllib.parse import quote, urlsplit

from reviewer.context.models import ISSUE_KEY_PATTERN


def allowed_url(url, base, *, base_path=True):
    try:
        parsed, origin = urlsplit(url), urlsplit(base)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and (parsed.scheme, parsed.hostname, parsed.port)
            == (origin.scheme, origin.hostname, origin.port)
            and not parsed.username
            and not parsed.password
            and not any(ord(c) < 32 for c in url)
            and "\\" not in url
            and (not base_path or parsed.path.startswith(origin.path.rstrip("/") + "/"))
        )
    except (ValueError, TypeError):
        return False


def review_links(snapshot, overrides, settings, mr=None):
    bundle = snapshot.get("bundle") or {}
    overrides = overrides or {}
    links = []
    seen = set()

    def add(kind, label, url, base, *, base_path=True):
        if url and url not in seen and allowed_url(url, base, base_path=base_path):
            seen.add(url)
            links.append({"kind": kind, "label": label, "url": url})

    source_mr = mr or bundle.get("mr") or {}
    add(
        "gitlab",
        "GitLab merge request",
        source_mr.get("web_url"),
        settings.gitlab_base_url,
    )
    keys = [
        (
            "Jira issue",
            overrides.get("issue_key")
            or (bundle.get("issue") or {}).get("key")
            or (bundle.get("linkage") or {}).get("issue_key"),
        ),
        (
            "Jira epic",
            overrides.get("epic_key") or (bundle.get("epic") or {}).get("key"),
        ),
    ]
    keys += [
        ("Jira issue", key)
        for key in (bundle.get("linkage") or {}).get("secondary_keys", [])
    ]
    for label, key in keys:
        if key and re.fullmatch(ISSUE_KEY_PATTERN, key):
            add(
                "jira",
                f"{label}: {key}",
                f"{settings.jira_base_url.rstrip('/')}/browse/{quote(key, safe='')}",
                settings.jira_base_url,
            )
    subject = bundle.get("subject") or {}
    if subject.get("url"):
        add(
            "confluence",
            f"Reviewed page: {subject.get('title') or subject.get('page_id')}",
            subject.get("url"),
            settings.confluence_base_url,
            base_path=False,
        )
    for page in bundle.get("documents", []):
        add(
            "confluence",
            page.get("title") or "Confluence page",
            page.get("url"),
            settings.confluence_base_url,
            base_path=False,
        )
    for url in overrides.get("document_urls", []) + overrides.get(
        "supporting_urls", []
    ):
        add(
            "confluence",
            "Confluence page",
            url,
            settings.confluence_base_url,
            base_path=False,
        )
    return links
