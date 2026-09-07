from reviewer.context.linkage import resolve
from reviewer.context.models import Linkage, ReviewOverrides


async def epic_by_key(issues, key):
    """Fetch an epic named directly by an operator.

    `get_epic_for` walks from a story to its parent; a manually supplied epic has
    no story to walk from, so it is read as an ordinary issue and narrowed.
    """
    from reviewer.services.issues.jira import EpicContext

    issue = await issues.get_issue(key)
    return (
        EpicContext(
            key=issue.key, summary=issue.summary, description_md=issue.description_md
        )
        if issue
        else None
    )


async def collect(mr, config, issues, docs, commits=(), overrides=None):
    overrides = overrides or ReviewOverrides()
    linkage = (
        Linkage(issue_key=overrides.issue_key, resolved_from="manual")
        if overrides.issue_key
        else resolve(mr, config.issue_tracker.project_keys, commits)
    )
    degradations = []
    issue = epic = None
    pages = []
    if linkage.issue_key:
        try:
            issue = await issues.get_issue(linkage.issue_key)
            # A manual epic key wins over the one derived from the story.
            if issue and not overrides.epic_key:
                epic = await issues.get_epic_for(issue.key)
        except Exception:
            degradations.append("jira_unavailable")
    if overrides.epic_key:
        try:
            epic = await epic_by_key(issues, overrides.epic_key)
        except Exception:
            degradations.append("jira_unavailable")
        if epic is None:
            linkage.warnings.append(
                f"{overrides.epic_key}: epic missing or inaccessible"
            )
    if not issue:
        if linkage.issue_key:
            linkage.warnings.append(f"{linkage.issue_key}: missing or inaccessible")
        linkage.issue_key = None
        degradations.append("unlinked")
        # Operator-supplied pages and epics stand on their own: an unlinked
        # change still gets whatever requirements were handed to it by hand.
        if not overrides.document_urls and not epic:
            return linkage, issue, epic, pages, degradations
    if issue:
        from reviewer.context.conversion import criteria

        issue.acceptance_criteria = criteria(
            issue.description_md, config.issue_tracker.ac_heading
        )
        if config.issue_tracker.include_comments and hasattr(issues, "comments"):
            issue.description_md += (
                "\n\nIssue comments (untrusted):\n" + await issues.comments(issue.key)
            )
    remaining = config.documents.max_tokens
    try:
        # Manual links lead so the page and token budgets cannot be spent on
        # links discovered from the issue before the requested ones are read.
        links = list(overrides.document_urls)
        if issue:
            links += await issues.get_document_links(issue.key)
        if epic:
            links += await issues.get_document_links(epic.key)
        if issue and config.issue_tracker.multi_issue:
            for key in linkage.secondary_keys:
                secondary = await issues.get_issue(key)
                if secondary:
                    links += await issues.get_document_links(key)
        pending = [(url, 0) for url in dict.fromkeys(links)]
        seen = set()
        while pending:
            url, depth = pending.pop(0)
            ref = await docs.resolve(url)
            if not ref or ref.page_id in seen:
                continue
            seen.add(ref.page_id)
            if len(pages) >= config.documents.max_pages or remaining <= 0:
                degradations.append("docs_truncated")
                break
            page = await docs.fetch(ref)
            if not page:
                degradations.append("docs_unavailable")
                continue
            if len(page.text_md) > remaining:
                # Preserve all headings with a proportional slice of each section.
                import re

                sections = re.split(r"(?=^#{1,6} )", page.text_md, flags=re.M)
                allowance = max(0, remaining // max(1, len(sections)))
                page.text_md = "\n".join(s[:allowance] for s in sections)
                page.truncated = True
                degradations.append("docs_truncated")
            remaining -= len(page.text_md)
            pages.append(page)
            if depth < config.documents.child_depth and hasattr(docs, "children"):
                pending.extend(
                    (child.url, depth + 1) for child in await docs.children(ref)
                )
    except Exception:
        degradations.append("docs_unavailable")
    return linkage, issue, epic, pages, list(dict.fromkeys(degradations))
