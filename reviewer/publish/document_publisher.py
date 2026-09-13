"""Publish a document review to its Confluence page, or hold it as drafts.

"applied" posts one footer comment per finding — inline first when the operator
enabled it — and a summary comment; "draft" stores the same comments as
`drafted` rows for the owner to publish from the dashboard, writing nothing to
Confluence; "none" stores the report only. Silent enforcement writes nothing
under any mode, drafts included. Reconciliation is by fingerprint marker, so a
rerun never posts the same finding twice.
"""

import structlog

from reviewer.findings.dedup import ORDER
from reviewer.findings.models import Severity
from reviewer.publish.comments import entry
from reviewer.publish.confluence_render import (
    FINGERPRINT,
    SUMMARY,
    finding_body,
    summary_body,
    summary_markdown,
)
from reviewer.publish.publisher import owns_body
from reviewer.services.docs.confluence import InlineUnsupported

logger = structlog.get_logger()

# Severities that earn their own comment; the rest are summary-only.
COMMENTABLE = {Severity.BLOCKER, Severity.REQUIRED, Severity.SUGGESTION}


def publishable(findings):
    return [
        f
        for f in findings
        if f.status not in {"discarded", "suppressed"} and f.severity_final is not None
    ]


def select(findings, max_comments):
    """Findings that get their own comment, worst first; the overflow count."""
    candidates = sorted(
        (f for f in findings if f.severity_final in COMMENTABLE),
        key=lambda f: ORDER[f.severity_final],
    )
    return candidates[:max_comments], max(0, len(candidates) - max_comments)


def ours(docs, comments, principal_id):
    """Comments this reviewer posted for this owner, keyed by fingerprint."""
    by_fingerprint, summaries = {}, []
    for c in comments:
        if str(c.author) != str(principal_id) or not owns_body(docs, c.body_storage):
            continue
        for fp in FINGERPRINT.findall(c.body_storage):
            by_fingerprint[fp] = c
        if SUMMARY.search(c.body_storage):
            summaries.append(c)
    return by_fingerprint, summaries


class DocumentPublisher:
    def __init__(self, docs, store):
        self.docs, self.store = docs, store

    async def publish(
        self,
        review,
        subject,
        findings,
        decision,
        config,
        report_mode,
        degradations,
        partial,
    ):
        findings = publishable(findings)
        writes_allowed = config.enforcement != "silent"
        mode = report_mode or ("applied" if writes_allowed else "none")
        if mode == "applied" and not writes_allowed:
            mode = "none"
        if mode == "draft" and not writes_allowed:
            # A drafted run under silent enforcement renders and writes nothing.
            mode = "none"
        inline, overflow = select(findings, config.document_review.max_comments)
        summary_md = summary_markdown(
            subject, findings, degradations, partial, decision
        )
        report = {
            "mode": mode,
            "summary": summary_md,
            "inline": [
                {
                    "fingerprint": f.fingerprint,
                    "body": finding_body(f),
                    "page_id": f.anchor.page_id,
                    "heading_path": f.anchor.heading_path,
                    "quote": f.anchor.quote,
                    "severity": str(f.severity_final),
                }
                for f in inline
            ],
            "overflow": overflow,
        }
        if mode == "none":
            logger.info("document_publication_skipped", review_id=review.id, mode=mode)
            return report
        page_id, version = subject["page_id"], subject["version"]
        summary_storage = summary_body(
            subject, findings, inline, overflow, degradations, partial, decision
        )
        report["summary_storage"] = summary_storage
        rows = await self.store.comments_for(review.id)

        async def remember(key, body, **fields):
            row = (rows.get(key) or entry(key, body)) | {"body": body} | fields
            rows[key] = row
            await self.store.save_comment(review.id, key, row)

        if mode == "draft":
            for item in report["inline"]:
                await remember(
                    item["fingerprint"],
                    item["body"],
                    status="drafted",
                    page_id=item["page_id"],
                    heading_path=item["heading_path"],
                    quote=item["quote"],
                    location="footer",
                    file=item["page_id"],
                )
            await remember(
                "summary", summary_storage, status="drafted", page_id=page_id
            )
            logger.info(
                "document_publication_drafted",
                review_id=review.id,
                comments=len(report["inline"]),
            )
            return report
        # Applied: reconcile what is already on the page before writing.
        existing = await self.docs.comments(page_id)
        posted, summaries = ours(self.docs, existing, review.principal_id)
        open_ours = [c for c in posted.values() if not c.resolved]
        slots = max(0, config.document_review.max_comments - len(open_ours))
        for item in report["inline"]:
            fp = item["fingerprint"]
            if fp in posted:
                comment = posted[fp]
                await remember(
                    fp,
                    item["body"],
                    status="committed",
                    comment_id=comment.id,
                    comment_version=comment.version,
                    location=comment.location,
                    page_id=item["page_id"],
                    heading_path=item["heading_path"],
                    quote=item["quote"],
                    file=item["page_id"],
                    thread_status="not_applicable",
                )
                continue
            if slots <= 0:
                report["overflow"] += 1
                await remember(
                    fp,
                    item["body"],
                    status="not_published",
                    eligible=False,
                    page_id=item["page_id"],
                    heading_path=item["heading_path"],
                    quote=item["quote"],
                    file=item["page_id"],
                )
                continue
            await remember(
                fp,
                item["body"],
                status="not_published",
                intent="publish",
                page_id=item["page_id"],
                heading_path=item["heading_path"],
                quote=item["quote"],
                file=item["page_id"],
            )
            comment = await post(
                self.docs, config, page_id, item["body"], item["quote"]
            )
            slots -= 1
            await remember(
                fp,
                item["body"],
                status="committed",
                intent=None,
                comment_id=comment.id,
                comment_version=comment.version,
                location=comment.location,
                thread_status="not_applicable",
            )
        current = [
            s
            for s in summaries
            if SUMMARY.search(s.body_storage).groups()
            == (str(int(page_id)), str(int(version)))
        ]
        if current:
            await remember(
                "summary",
                summary_storage,
                status="committed",
                comment_id=current[0].id,
                comment_version=current[0].version,
                page_id=page_id,
            )
        else:
            await remember(
                "summary",
                summary_storage,
                status="not_published",
                intent="publish",
                page_id=page_id,
            )
            comment = await self.docs.post_comment(page_id, summary_storage)
            await remember(
                "summary",
                summary_storage,
                status="committed",
                intent=None,
                comment_id=comment.id,
                comment_version=comment.version,
                page_id=page_id,
            )
        logger.info(
            "document_publication_done",
            review_id=review.id,
            comments=len(report["inline"]),
            overflow=report["overflow"],
        )
        return report


async def post(docs, config, page_id, body, quote):
    """A footer comment, after one inline attempt when the operator asked for it."""
    if config.document_review.inline_comments and quote:
        try:
            return await docs.post_inline_comment(page_id, body, quote)
        except InlineUnsupported:
            logger.info("inline_comment_unsupported", page_id=page_id)
    return await docs.post_comment(page_id, body)
