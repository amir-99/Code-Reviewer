"""Reconcile and manage only the messages attributable to a stored review.

Callers hold Store.mr_lock across reconciliation and every remote mutation.
Remote IDs and deletion intents survive lost responses and process restarts.
"""

import hashlib
import re
from datetime import UTC, datetime

import httpx

from reviewer.context.redaction import Redactor
from reviewer.publish.publisher import FINGERPRINT, owns_body
from reviewer.services.forge.gitlab import Position

MARKERS = re.compile(r"<!-- ai-review:[^>]* -->")
FOOTER = re.compile(r"<sub>AI review.*?</sub>", re.S)


class CommentConflict(ValueError):
    pass


def revision(body):
    return hashlib.sha256(body.encode()).hexdigest()


def editable(body):
    return FOOTER.sub("", MARKERS.sub("", body)).strip()


def edited(body, replacement):
    if MARKERS.search(replacement) or FOOTER.search(replacement):
        raise CommentConflict("Comment identity markers cannot be edited")
    return "\n\n".join(
        [Redactor().text(replacement.strip())]
        + FOOTER.findall(body)
        + MARKERS.findall(body)
    )


def entry(key, body, **extra):
    return {
        "key": key,
        "body": body,
        "status": "not_published",
        "thread_status": "not_applicable",
        **extra,
    }


class Comments:
    def __init__(self, store, forge, review, project, config):
        self.store, self.forge, self.review = store, forge, review
        self.project, self.iid, self.config = project, review.mr_iid, config
        self.rows = {}
        self.discussions, self.drafts = [], []

    async def save(self, row):
        self.rows[row["key"]] = row
        await self.store.save_comment(self.review.id, row["key"], row)

    async def seed(self):
        self.rows = await self.store.comments_for(self.review.id)
        snapshot = await self.store.snapshot(self.review.id) or {}
        report = snapshot.get("report") or {}
        previous_status = {"draft": "drafted", "applied": "committed"}.get(
            report.get("mode"), "not_published"
        )
        for item in report.get("inline", []):
            key = item["fingerprint"]
            if key not in self.rows:
                await self.save(
                    entry(
                        key,
                        item["body"],
                        status=previous_status,
                        position=item.get("position"),
                        file=item.get("file"),
                    )
                )
        if report.get("summary") and "summary" not in self.rows:
            await self.save(entry("summary", report["summary"], status=previous_status))
        # Older reports omitted deduplicated inline comments. Findings supply
        # identities for reconciliation, but never make discarded findings publishable.
        for finding in await self.store.findings_for(self.review.id):
            key = finding.get("fingerprint")
            if key and key not in self.rows:
                await self.save(
                    entry(key, "", file=(finding.get("anchor") or {}).get("file"))
                )

    def marker(self, key):
        if key == "summary":
            return f"<!-- ai-review:summary={self.review.head_sha} -->"
        return f"<!-- ai-review:fingerprint={key} -->"

    async def sync(self):
        await self.seed()
        bot = await self.forge.identity()
        # Drafts first: publication between reads can be reconciled as committed.
        self.drafts = await self.forge.list_draft_notes(self.project, self.iid)
        self.discussions = await self.forge.list_discussions(self.project, self.iid)
        now = datetime.now(UTC).isoformat()
        for key, previous in list(self.rows.items()):
            row = dict(previous)
            marker = self.marker(key)
            matches = [
                (d, n)
                for d in self.discussions
                for n in d.notes
                if n.author_id == bot
                and owns_body(self.forge, n.body)
                and marker in n.body
                and "<!-- ai-review:recheck=" not in n.body
            ]
            drafts = [
                n
                for n in self.drafts
                if owns_body(self.forge, n.body)
                and marker in n.body
                and n.author_id in {0, bot}
                and not n.discussion_id
            ]
            # A receipt disambiguates repeated findings on historical heads.
            exact = [(d, n) for d, n in matches if n.id == row.get("note_id")]
            if row.get("note_id") is not None:
                matches = exact
            elif len(matches) > 1:
                opened = [(d, n) for d, n in matches if not d.resolved]
                if len(opened) == 1:
                    matches = opened
            if (
                row.get("removed_by_owner")
                and row.get("status") == "removed"
                and not row.get("intent")
            ):
                row["synced_at"] = now
                self.rows[key] = row
                continue
            identity_changed = (
                not matches
                and any(
                    n.id == row.get("note_id")
                    for d in self.discussions
                    for n in d.notes
                )
            ) or (not drafts and any(n.id == row.get("draft_id") for n in self.drafts))
            if identity_changed:
                row["conflict"] = "Comment identity changed; inspect it in GitLab"
            elif len(matches) > 1 or len(drafts) > 1 or (matches and drafts):
                row["conflict"] = (
                    "Multiple matching comments; reconcile in GitLab first"
                )
            else:
                row.pop("conflict", None)
                if matches:
                    discussion, note = matches[0]
                    if row.get("intent") == "publish":
                        row.pop("intent", None)
                    row.update(
                        status="committed",
                        body=Redactor().text(note.body),
                        note_id=note.id,
                        discussion_id=discussion.id,
                        draft_id=None,
                        thread_status=("resolved" if discussion.resolved else "open")
                        if discussion.resolvable
                        else "not_applicable",
                    )
                elif drafts:
                    note = drafts[0]
                    row.update(
                        status="drafted",
                        body=Redactor().text(note.body),
                        draft_id=note.id,
                        note_id=None,
                        discussion_id=None,
                        thread_status="not_applicable",
                    )
                elif (
                    row.get("status") in {"drafted", "committed", "removed"}
                    or row.get("intent") == "remove"
                ):
                    row.update(status="removed", thread_status="not_applicable")
                    row.pop("intent", None)
                elif row.get("intent") == "publish":
                    # A successful write may be temporarily invisible. Never recreate
                    # a comment after an ambiguous publish response.
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
        p, i = self.project, self.iid
        status = row["status"]
        if action == "resolve":
            if status != "committed" or row["thread_status"] != "open":
                return "skipped"
            await self.forge.resolve_discussion(p, i, row["discussion_id"])
            row["thread_status"] = "resolved"
        elif action == "edit":
            if status not in {"drafted", "committed"}:
                raise CommentConflict(
                    "Only drafted or committed messages can be edited"
                )
            body = edited(row["body"], message)
            if status == "drafted":
                await self.forge.edit_draft_note(p, i, row["draft_id"], body)
            else:
                await self.forge.edit_note(p, i, row["note_id"], body)
            row["body"] = body
        elif action == "remove":
            if status == "removed":
                return "skipped"
            if status not in {"drafted", "committed"}:
                raise CommentConflict("No remote comment to remove")
            row["intent"] = "remove"
            row["removed_by_owner"] = True
            await self.save(row)
            if status == "drafted":
                await self.forge.delete_draft_note(p, i, row["draft_id"])
            else:
                # Delete this note only; never delete replies written by others.
                await self.forge.delete_note(p, i, row["note_id"])
            row.update(status="removed", thread_status="not_applicable")
        elif action == "publish":
            if status in {"committed", "removed"}:
                return "skipped"
            if (
                row.get("eligible") is False
                or not row["body"]
                or (
                    key != "summary" and status != "drafted" and not row.get("position")
                )
            ):
                return "skipped"
            if key != "summary":
                # Include existing reviewer threads and other pending drafts in caps.
                opened = [
                    d
                    for d in self.discussions
                    if not d.resolved
                    and any(FINGERPRINT.search(n.body) for n in d.notes)
                ]
                pending = [
                    n
                    for n in self.drafts
                    if FINGERPRINT.search(n.body) and n.id != row.get("draft_id")
                ]
                if len(opened) + len(pending) >= min(
                    15, self.config.review.max_inline
                ) or sum(d.file == row.get("file") for d in opened + pending) >= min(
                    5, self.config.review.max_per_file
                ):
                    raise CommentConflict("Inline comment limit reached")
            if status == "drafted":
                await self.forge.publish_draft_note(p, i, row["draft_id"])
            else:
                row["intent"] = "publish"
                await self.save(row)
                try:
                    if key == "summary":
                        note = await self.forge.post_note(p, i, row["body"])
                        row["note_id"] = note.id
                    else:
                        discussion = await self.forge.post_inline_discussion(
                            p, i, row["body"], Position.model_validate(row["position"])
                        )
                        row.update(
                            discussion_id=discussion.id, note_id=discussion.notes[0].id
                        )
                except httpx.HTTPStatusError as exc:
                    # A definitive client rejection did not create a note. Keep
                    # ambiguous transport/server outcomes pinned for reconciliation.
                    if (
                        400 <= exc.response.status_code < 500
                        and exc.response.status_code != 408
                    ):
                        row.pop("intent", None)
                        await self.save(row)
                    raise
            row["status"] = "committed"
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
