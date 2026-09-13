"""Owner comment controls for a document review, over Confluence page comments.

Same rows and verbs as `Comments` for merge requests — sync, edit, remove,
publish — with the page's comments as the remote truth. Drafted rows exist only
here: publishing one posts it. Resolving is not offered; Confluence Data Center
documents no endpoint for it.
"""

import re
from datetime import UTC, datetime
from html import escape

import httpx

from reviewer.context.conversion import markdown
from reviewer.context.redaction import Redactor
from reviewer.publish.comments import CommentConflict, entry, revision
from reviewer.publish.confluence_render import (
    FINGERPRINT,
    SUMMARY,
    fingerprint_marker,
    footer,
    summary_marker,
)
from reviewer.publish.document_publisher import post
from reviewer.publish.publisher import owns_body

MARKERS = re.compile(r"<!-- ai-review:[^>]* -->")
FOOTER = re.compile(r"<p><sub>AI review.*?</sub></p>", re.S)


def editable(body):
    """The owner-editable text of a storage body, as Markdown."""
    return markdown(FOOTER.sub("", MARKERS.sub("", body))).strip()


def storage_from_message(message):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    return "\n".join(
        "<p>" + escape(Redactor().text(p), quote=False).replace("\n", "<br/>") + "</p>"
        for p in paragraphs
    )


def edited(body, message, key, page_id, version):
    if MARKERS.search(message) or FOOTER.search(message):
        raise CommentConflict("Comment identity markers cannot be edited")
    marker = (
        summary_marker(page_id, version)
        if key == "summary"
        else fingerprint_marker(key)
    )
    return "\n".join([storage_from_message(message), footer(), marker])


class DocumentComments:
    def __init__(self, store, docs, review, config):
        self.store, self.docs, self.review, self.config = store, docs, review, config
        self.subject = review.subject or {}
        self.page_id = str(self.subject.get("page_id"))
        self.rows = {}
        self.remote = []

    async def save(self, row):
        self.rows[row["key"]] = row
        await self.store.save_comment(self.review.id, row["key"], row)

    async def seed(self):
        self.rows = await self.store.comments_for(self.review.id)
        snapshot = await self.store.snapshot(self.review.id) or {}
        report = snapshot.get("report") or {}
        previous = {"draft": "drafted", "applied": "committed"}.get(
            report.get("mode"), "not_published"
        )
        for item in report.get("inline", []):
            if item["fingerprint"] not in self.rows:
                await self.save(
                    entry(
                        item["fingerprint"],
                        item["body"],
                        status=previous,
                        page_id=item.get("page_id"),
                        heading_path=item.get("heading_path"),
                        quote=item.get("quote"),
                        file=item.get("page_id"),
                    )
                )
        if report.get("summary_storage") and "summary" not in self.rows:
            await self.save(
                entry(
                    "summary",
                    report["summary_storage"],
                    status=previous,
                    page_id=self.page_id,
                )
            )

    def _matches(self, key, principal):
        marker = (
            SUMMARY
            if key == "summary"
            else re.compile(re.escape(fingerprint_marker(key)))
        )
        return [
            c
            for c in self.remote
            if str(c.author) == str(principal)
            and owns_body(self.docs, c.body_storage)
            and marker.search(c.body_storage)
            and (
                key != "summary"
                or SUMMARY.search(c.body_storage).groups()
                == (str(int(self.page_id)), str(int(self.subject.get("version", 0))))
            )
        ]

    async def sync(self):
        await self.seed()
        principal = await self.docs.identity()
        self.remote = await self.docs.comments(self.page_id)
        now = datetime.now(UTC).isoformat()
        for key, previous in list(self.rows.items()):
            row = dict(previous)
            matches = self._matches(key, principal)
            if (
                row.get("removed_by_owner")
                and row.get("status") == "removed"
                and not row.get("intent")
            ):
                row["synced_at"] = now
                self.rows[key] = row
                continue
            if len(matches) > 1:
                row["conflict"] = (
                    "Multiple matching comments; reconcile in Confluence first"
                )
            elif not matches and any(
                c.id == row.get("comment_id") for c in self.remote
            ):
                row["conflict"] = "Comment identity changed; inspect it in Confluence"
            else:
                row.pop("conflict", None)
                if matches:
                    comment = matches[0]
                    row.pop("intent", None)
                    row.update(
                        status="committed",
                        body=Redactor().text(comment.body_storage),
                        comment_id=comment.id,
                        comment_version=comment.version,
                        location=comment.location,
                        thread_status="not_applicable",
                    )
                elif (
                    row.get("status") in {"committed", "removed"}
                    or row.get("intent") == "remove"
                ):
                    row.update(status="removed", thread_status="not_applicable")
                    row.pop("intent", None)
                    row.pop("comment_id", None)
                elif row.get("intent") == "publish":
                    row["conflict"] = (
                        "Publication outcome unknown; refresh before retrying"
                    )
            row["synced_at"] = now
            self.rows[key] = row
        await self.store.save_comments(self.review.id, self.rows)
        return self.view()

    def view(self):
        return [
            {**row, "message": editable(row["body"]), "revision": revision(row["body"])}
            for row in self.rows.values()
        ]

    async def act(self, key, action, expected=None, message=None):
        await self.sync()
        row = dict(self.rows.get(key) or {})
        if not row:
            raise CommentConflict("Comment not found")
        if row.get("conflict"):
            raise CommentConflict(row["conflict"])
        if expected is not None and expected != revision(row["body"]):
            raise CommentConflict("Comment changed; refresh before editing")
        if row.get("intent") == "remove" and action != "remove":
            raise CommentConflict("Removal is pending; retry removal first")
        status = row["status"]
        version = self.subject.get("version", 0)
        if action == "resolve":
            raise CommentConflict("Resolving Confluence comments is not supported")
        elif action == "edit":
            if status not in {"drafted", "committed"}:
                raise CommentConflict(
                    "Only drafted or committed messages can be edited"
                )
            body = edited(row["body"], message, key, self.page_id, version)
            if status == "committed":
                comment = await self.docs.edit_comment(
                    row["comment_id"], body, row.get("comment_version", 1)
                )
                row["comment_version"] = comment.version
            row["body"] = body
        elif action == "remove":
            if status == "removed":
                return "skipped"
            if status not in {"drafted", "committed"}:
                raise CommentConflict("No comment to remove")
            row["intent"] = "remove"
            row["removed_by_owner"] = True
            await self.save(row)
            if status == "committed":
                await self.docs.delete_comment(row["comment_id"])
            row.update(status="removed", thread_status="not_applicable")
            row.pop("comment_id", None)
        elif action == "publish":
            if status in {"committed", "removed"}:
                return "skipped"
            if row.get("eligible") is False or not row["body"]:
                return "skipped"
            if key != "summary":
                open_ours = [
                    c
                    for c in self.remote
                    if FINGERPRINT.search(c.body_storage)
                    and owns_body(self.docs, c.body_storage)
                    and not c.resolved
                ]
                if len(open_ours) >= self.config.document_review.max_comments:
                    raise CommentConflict("Comment limit reached")
            row["intent"] = "publish"
            await self.save(row)
            try:
                if key == "summary":
                    comment = await self.docs.post_comment(self.page_id, row["body"])
                else:
                    comment = await post(
                        self.docs,
                        self.config,
                        self.page_id,
                        row["body"],
                        row.get("quote"),
                    )
            except httpx.HTTPStatusError as exc:
                if (
                    400 <= exc.response.status_code < 500
                    and exc.response.status_code != 408
                ):
                    row.pop("intent", None)
                    await self.save(row)
                raise
            row.update(
                status="committed",
                comment_id=comment.id,
                comment_version=comment.version,
                location=comment.location,
                thread_status="not_applicable",
            )
        else:
            raise CommentConflict("Unknown comment action")
        row.pop("intent", None)
        row["actions"] = row.get("actions", []) + [
            {
                "action": action,
                "at": datetime.now(UTC).isoformat(),
                "actor": self.review.owner_user_id,
            }
        ]
        row["last_action"] = action
        row["acted_at"] = datetime.now(UTC).isoformat()
        await self.save(row)
        return "succeeded"
