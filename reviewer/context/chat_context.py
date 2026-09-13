"""What a question about a review may read, and how it is handed to the model.

The stored record is the first source for everything: it is what the review
actually reasoned over, so an answer drawn from it explains the review rather
than a later state of the world. Code outside the diff, an issue the record
does not hold, or a page the record truncated are read directly — at the
reviewed commit, with the owner's own credentials — only when asked for.
"""

import json
import re
from contextlib import AsyncExitStack

from reviewer.context.excerpts import excerpt, shorten
from reviewer.context.framing import frame
from reviewer.context.redaction import Redactor

ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
PATH_LIKE = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,8}")
FILE_LIMIT = 12000
EVENT_LIMIT = 150


class ChatSources:
    def __init__(
        self,
        store,
        review,
        snapshot,
        config,
        *,
        git=None,
        repo_url="",
        issues=None,
        docs=None,
        scanner=None,
        redactor=None,
    ):
        self.store, self.review, self.config = store, review, config
        self.snapshot = snapshot or {}
        self.bundle = self.snapshot.get("bundle") or {}
        self.git, self.repo_url = git, repo_url
        self.issues, self.docs, self.scanner = issues, docs, scanner
        self.redactor = redactor or Redactor()
        self.stack = AsyncExitStack()
        self.cache = None
        self.symbols = None
        self.given_refs = {
            "file": set(),
            "finding": set(),
            "issue": set(),
            "page": set(),
        }
        self.reads = []

    # -- the fixed record -------------------------------------------------

    async def fixed(self):
        review, bundle = self.review, self.bundle
        findings = [self._finding(f) for f in await self.store.findings_for(review.id)]
        self.given_refs["finding"].update(f["id"] for f in findings if f["id"])
        issue, epic = bundle.get("issue"), bundle.get("epic")
        if issue:
            self.given_refs["issue"].add(issue.get("key"))
        if epic:
            self.given_refs["issue"].add(epic.get("key"))
        documents = [
            {
                "page_id": d.get("page_id"),
                "title": d.get("title"),
                "version": d.get("version"),
                "truncated": d.get("truncated"),
            }
            for d in bundle.get("documents") or []
        ]
        self.given_refs["page"].update(d["page_id"] for d in documents if d["page_id"])
        stages = {
            name: {
                "status": result.status,
                "partial": getattr(result, "partial", None),
                "error": getattr(result, "error", None),
            }
            for name, result in (await self.store.stages(review.id)).items()
        }
        mr = bundle.get("mr") or {}
        code = bundle.get("code") or {}
        history = await self.store.chat_messages(review.id)
        turns = [
            {"question": m["question"], "answer": m["answer"]}
            for m in history
            if m["status"] == "answered"
        ][-self.config.chat.history_turns :]
        return {
            "review": {
                "id": review.id,
                "state": review.state,
                "decision": review.decision,
                "partial": review.partial,
                "error": review.error,
                "head_sha": review.head_sha,
                "merge_base_sha": review.merge_base_sha,
                "models": (bundle.get("budget") or {}).get("model_tier"),
                "degradations": bundle.get("degradations") or [],
                "analysis_mode": (self.snapshot.get("config") or {}).get(
                    "analysis_mode"
                ),
            },
            "merge_request": {
                "iid": mr.get("iid"),
                "title": mr.get("title"),
                "description": mr.get("description"),
                "source_branch": mr.get("source_branch"),
                "target_branch": mr.get("target_branch"),
            },
            "changed_files": [
                {
                    "path": f.get("path"),
                    "change_type": f.get("change_type"),
                    "language": f.get("language"),
                    "changed_lines": sum(
                        1
                        for line in f.get("lines") or []
                        if line.get("kind") != "context"
                    ),
                    "excluded": bool(f.get("is_excluded") or f.get("is_generated")),
                }
                for f in code.get("files") or []
            ],
            "requirement": {
                "linkage": bundle.get("linkage"),
                "issue": issue,
                "epic": epic,
                "documents": documents,
            },
            "static_analysis": [
                {k: s.get(k) for k in ("name", "status", "exit_code", "required")}
                for s in bundle.get("static") or []
            ],
            "findings": findings,
            "report": self.snapshot.get("report"),
            "recheck": self.snapshot.get("recheck"),
            "stages": stages,
            "conversation": turns,
        }

    @staticmethod
    def _finding(finding):
        anchor = finding.get("anchor") or {}
        verification = finding.get("verification") or {}
        return {
            "id": finding.get("id"),
            "stage": finding.get("stage"),
            "category": finding.get("category"),
            "severity": finding.get("severity_final"),
            "severity_proposed": finding.get("severity_proposed"),
            "status": finding.get("status"),
            "confidence": finding.get("confidence"),
            "impact_level": finding.get("impact_level"),
            "claim": finding.get("claim"),
            "reason": finding.get("reason"),
            "failure_scenario": finding.get("failure_scenario"),
            "suggested_direction": finding.get("suggested_direction"),
            "requirement_ref": finding.get("requirement_ref"),
            "file": anchor.get("file"),
            "line_start": anchor.get("line_start"),
            "line_end": anchor.get("line_end"),
            "introduced_by_this_change": anchor.get("introduced_by_this_change"),
            "evidence": finding.get("evidence") or [],
            "verification": {
                "verdict": verification.get("verdict"),
                "reasoning": verification.get("reasoning"),
            }
            if verification
            else None,
            "resolution": finding.get("resolution"),
        }

    # -- what the question itself points at ---------------------------------

    async def prefetch(self, question):
        """Diff hunks, issues and symbols the question names, before any model asks."""
        found = {}
        files = {
            f.get("path"): f for f in (self.bundle.get("code") or {}).get("files") or []
        }
        for token in PATH_LIKE.findall(question):
            for path in files:
                if path == token or path.endswith("/" + token) or token in path:
                    found[f"diff:{path}"] = self._hunks(files[path])
                    break
        for key in ISSUE_KEY.findall(question):
            if key not in self.given_refs["issue"]:
                found[f"issue:{key}"] = await self._issue(key)
        # A finding's own hunk is the most likely thing a question is about.
        for finding in await self.store.findings_for(self.review.id):
            fid = finding.get("id") or ""
            if fid and fid in question:
                path = (finding.get("anchor") or {}).get("file")
                if path in files:
                    found[f"diff:{path}"] = self._hunks(files[path])
        return found

    def _hunks(self, file):
        self.given_refs["file"].add(file.get("path"))
        self._note(f"diff:{file.get('path')}")
        lines = []
        for line in file.get("lines") or []:
            mark = {"added": "+", "removed": "-"}.get(line.get("kind"), " ")
            number = line.get("new_line") or line.get("old_line") or ""
            lines.append(f"{number:>5} {mark} {line.get('text', '')}")
        text = "\n".join(lines)
        return {
            "path": file.get("path"),
            "change_type": file.get("change_type"),
            "diff": text[:FILE_LIMIT],
            "truncated": len(text) > FILE_LIMIT,
        }

    # -- what the model asks for --------------------------------------------

    async def provide(self, requests):
        output = {}
        for request in requests[:5]:
            key = f"{request.kind}:{request.target}".rstrip(":")
            try:
                if request.kind == "file":
                    output[key] = await self._file(
                        request.target, request.line_start, request.line_end
                    )
                elif request.kind == "symbol":
                    output.update(await self._symbol(request.target))
                elif request.kind == "issue":
                    output[key] = await self._issue(request.target)
                elif request.kind == "page":
                    output[key] = await self._page(request.target)
                elif request.kind == "events":
                    output["events"] = await self._events()
                elif request.kind == "comments":
                    output["comments"] = await self._comments()
            except Exception:
                output[key] = {"unavailable": True}
        return output

    async def _worktree(self):
        if self.cache is None:
            if not (self.git and self.repo_url and self.scanner):
                raise RuntimeError("Code is unavailable")
            from reviewer.context.code_cache import ScannedCodeCache

            project = await self.store.project_number(self.review)
            wt = await self.stack.enter_async_context(
                self.git.workspace(project, self.repo_url, self.review.head_sha)
            )
            self.cache = ScannedCodeCache(self.git, wt, self.scanner, self.redactor)
        return self.cache

    async def _file(self, path, start=None, end=None):
        if not path or path.startswith(("/", "-")) or ".." in path.split("/"):
            return {"unavailable": True}
        cache = await self._worktree()
        source = await cache.read(path)
        self.given_refs["file"].add(path)
        self._note(f"file:{path}")
        return excerpt(source.splitlines(), start or 1, end, limit=FILE_LIMIT)

    async def _symbol(self, name):
        from reviewer.services.symbols.index import SymbolIndex

        if self.symbols is None:
            self.symbols = SymbolIndex()
            for file in (self.bundle.get("code") or {}).get("files") or []:
                path = file.get("path")
                if file.get("change_type") == "deleted" or not path:
                    continue
                try:
                    self.symbols.add(path, await (await self._worktree()).read(path))
                except Exception:
                    continue
        output = {}
        for path, symbols in self.symbols.symbols.items():
            for symbol in symbols:
                if symbol["name"] == name:
                    output[f"symbol:{path}:{name}"] = await self._file(
                        path, symbol["start"], symbol["end"]
                    )
                    break
            if len(output) >= 3:
                break
        return output or {f"symbol:{name}": {"unavailable": True}}

    async def _issue(self, key):
        if not ISSUE_KEY.fullmatch(key or ""):
            return {"unavailable": True}
        for stored in (self.bundle.get("issue"), self.bundle.get("epic")):
            if stored and stored.get("key") == key:
                self.given_refs["issue"].add(key)
                return stored
        if self.issues is None:
            return {"unavailable": "no Jira credential"}
        issue = await self.issues.get_issue(key)
        if issue is None:
            return {"unavailable": True}
        data = issue.model_dump(mode="json")
        if hasattr(self.issues, "comments"):
            try:
                data["comments"] = await self.issues.comments(key)
            except Exception:
                data["comments"] = None
        self.given_refs["issue"].add(key)
        self._note(f"issue:{key}")
        return data

    async def _page(self, page_id):
        stored = next(
            (
                d
                for d in self.bundle.get("documents") or []
                if str(d.get("page_id")) == str(page_id)
            ),
            None,
        )
        if stored is None:
            # Only pages the review itself linked: the chat cannot browse.
            return {"unavailable": True}
        self.given_refs["page"].add(str(page_id))
        if not stored.get("truncated") or self.docs is None:
            return stored
        from reviewer.services.docs.confluence import DocumentRef

        page = await self.docs.fetch(
            DocumentRef(page_id=str(page_id), url=stored.get("url", ""))
        )
        if page is None:
            return stored
        self._note(f"page:{page_id}")
        data = page.model_dump(mode="json")
        data["text_md"] = data["text_md"][: self.config.documents.max_tokens * 4]
        return data

    async def _events(self):
        self._note("events")
        rows = await self.store.events(self.review.id, 0, limit=2000)
        rows = rows[-EVENT_LIMIT:]
        return [
            {
                "at": row["at"],
                "kind": row["kind"],
                **{
                    k: row["data"].get(k)
                    for k in ("state", "name", "status", "stage", "outcome")
                    if k in row["data"]
                },
            }
            for row in rows
        ]

    async def _comments(self):
        self._note("comments")
        rows = await self.store.comments_for(self.review.id)
        return {
            key: {
                k: value.get(k)
                for k in ("status", "intent", "thread_status", "eligible", "file")
            }
            for key, value in rows.items()
        }

    # -- bookkeeping ---------------------------------------------------------

    def _note(self, what):
        if what not in self.reads:
            self.reads.append(what)

    def given(self):
        return self.given_refs

    def used(self):
        return list(self.reads)

    def render(self, context, question, limit):
        """The user turn: the record, then what was requested, then the question.

        Everything from the record is untrusted. Requested material is trimmed
        first when the prompt exceeds the model's allowance, intact excerpts
        before anything else, so the record itself is the last thing to go.
        """
        requested = dict(context.get("requested") or {})
        while True:
            body = "\n".join(
                [
                    frame(
                        json.dumps(
                            {k: v for k, v in context.items() if k != "requested"},
                            ensure_ascii=False,
                            default=str,
                        ),
                        "review-record",
                    ),
                    frame(
                        json.dumps(requested, ensure_ascii=False, default=str),
                        "requested-material",
                    ),
                    "Question from the review's owner:",
                    frame(question, "user-question"),
                ]
            )
            if len(body.encode()) <= limit or not requested:
                return self.redactor.text(body)
            largest = max(
                requested, key=lambda k: len(json.dumps(requested[k], default=str))
            )
            value = requested[largest]
            if (
                isinstance(value, dict)
                and "code" in value
                and value["code"].count("\n") > 1
            ):
                requested[largest] = shorten(value)
            else:
                requested.pop(largest)

    async def close(self):
        await self.stack.aclose()
