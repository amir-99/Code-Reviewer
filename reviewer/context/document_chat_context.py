"""What a question about a document review may read.

The stored record first, as for a merge request. Page text is not in the
record: when the model asks for a section or a search, the pages the review
read are fetched again with the owner's own Confluence token — at their current
version, which the answer states — scanned and redacted, and searched locally.
"""

from reviewer.context.chat_context import ChatSources, words
from reviewer.context.documents import DocumentCorpus
from reviewer.context.models import ReviewOverrides

SECTION_LIMIT = 12000


class DocumentChatSources(ChatSources):
    def __init__(
        self, store, review, snapshot, config, *, docs=None, scanner=None, redactor=None
    ):
        super().__init__(
            store,
            review,
            snapshot,
            config,
            docs=docs,
            scanner=scanner,
            redactor=redactor,
        )
        self.given_refs["page"] = set()
        self.corpus = None
        self.corpus_error = None

    async def fixed(self):
        review, bundle = self.review, self.bundle
        findings = [self._finding(f) for f in await self.store.findings_for(review.id)]
        self.given_refs["finding"].update(f["id"] for f in findings if f["id"])
        documents = [
            {k: d.get(k) for k in ("page_id", "title", "role", "version", "truncated")}
            for d in bundle.get("documents") or []
        ]
        self.given_refs["page"].update(
            str(d["page_id"]) for d in documents if d["page_id"]
        )
        stages = {
            name: {
                "status": "failed"
                if result.failed
                else "partial"
                if result.partial
                else "completed",
                "units_examined": len(result.examined),
                "units_skipped": len(result.skipped),
                "notes": result.notes[-20:],
                "attempts": result.attempts,
            }
            for name, result in (await self.store.stages(review.id)).items()
        }
        history = await self.store.chat_messages(review.id)
        turns = [
            {"question": m["question"], "answer": m["answer"]}
            for m in history
            if m["status"] == "answered"
        ][-self.config.chat.history_turns :]
        return {
            "review": {
                "id": review.id,
                "kind": "document",
                "state": review.state,
                "decision": review.decision,
                "partial": review.partial,
                "error": review.error,
                "models": (bundle.get("budget") or {}).get("model_tier"),
                "degradations": bundle.get("degradations") or [],
                "instruction": bundle.get("instruction"),
                "check_space": bundle.get("check_space"),
            },
            "subject": bundle.get("subject"),
            "documents": documents,
            "outline": [s.get("heading_path") for s in bundle.get("sections") or []],
            "space_inventory": (bundle.get("space") or {}).get("inventory", [])[:50],
            "findings": findings,
            "report": (self.snapshot.get("report") or {}).get("summary"),
            "stages": stages,
            "conversation": turns,
        }

    @staticmethod
    def _finding(finding):
        anchor = finding.get("anchor") or {}
        verification = finding.get("verification") or {}
        return {
            "id": finding.get("id"),
            "category": finding.get("category"),
            "severity": finding.get("severity_final"),
            "severity_proposed": finding.get("severity_proposed"),
            "status": finding.get("status"),
            "confidence": finding.get("confidence"),
            "impact_level": finding.get("impact_level"),
            "claim": finding.get("claim"),
            "reason": finding.get("reason"),
            "suggested_direction": finding.get("suggested_direction"),
            "page_id": anchor.get("page_id"),
            "heading_path": anchor.get("heading_path"),
            "quote": anchor.get("quote"),
            "related": finding.get("related") or [],
            "verification": {
                "verdict": verification.get("verdict"),
                "reasoning": verification.get("reasoning"),
            }
            if verification
            else None,
            "resolution": finding.get("resolution"),
        }

    async def prefetch(self, question):
        """The sections the question's words point at, before any model asks."""
        found = {}
        asked = words(question)
        for finding in await self.store.findings_for(self.review.id):
            fid = finding.get("id") or ""
            claim = words(finding.get("claim") or "")
            anchor = finding.get("anchor") or {}
            if (fid and fid in question) or (
                claim and len(claim & asked) >= max(3, 0.6 * len(claim))
            ):
                try:
                    value = await self._section(
                        anchor.get("page_id"), anchor.get("heading_path")
                    )
                except Exception:
                    continue
                if value and not value.get("unavailable"):
                    found[
                        f"section:{anchor.get('page_id')}|{anchor.get('heading_path')}"
                    ] = value
        return found

    async def provide(self, requests):
        output = {}
        for request in requests[:5]:
            key = f"{request.kind}:{request.target}".rstrip(":")
            try:
                if request.kind == "search":
                    page_id, _, query = request.target.partition("|")
                    if not query:
                        page_id, query = None, request.target
                    output[key] = await self._search(query, page_id or None)
                elif request.kind == "section":
                    page_id, _, heading = request.target.partition("|")
                    output[key] = await self._section(page_id.strip(), heading.strip())
                elif request.kind == "page":
                    output[key] = await self._outline(request.target)
                elif request.kind == "events":
                    output["events"] = await self._events()
                elif request.kind == "comments":
                    output["comments"] = await self._comments()
                else:
                    output[key] = {"unavailable": "not available for a document review"}
            except Exception:
                output[key] = {"unavailable": True}
        return output

    async def _load(self):
        if self.corpus is not None:
            return self.corpus
        if self.corpus_error:
            raise RuntimeError(self.corpus_error)
        if self.docs is None:
            self.corpus_error = "no Confluence credential"
            raise RuntimeError(self.corpus_error)
        subject = dict(self.bundle.get("subject") or {})
        # The page may have moved on; the chat reads what is there now and says so.
        subject.pop("version", None)
        corpus = DocumentCorpus(subject, self.redactor)
        overrides = ReviewOverrides.model_validate(self.review.overrides or {})
        await corpus.collect(self.docs, overrides, self.config, self.scanner)
        self.corpus = corpus
        return corpus

    def _cite(self, page_id, heading=None):
        self.given_refs["page"].add(str(page_id))
        if heading:
            self.given_refs["page"].add(f"{page_id}#{heading}")

    async def _search(self, query, page_id=None):
        corpus = await self._load()
        self._note(f"search:{query}")
        hits = corpus.search(query, page_id)
        for hit in hits:
            self._cite(hit["page_id"], hit["heading_path"])
        return {
            "current_versions": {p.page_id: p.version for p in corpus.pages.values()},
            "hits": hits,
        }

    async def _outline(self, page_id):
        corpus = await self._load()
        outline = corpus.outline(page_id)
        if outline is None:
            return {"unavailable": True}
        self._cite(page_id)
        self._note(f"page:{page_id}")
        return outline

    async def _section(self, page_id, heading_path):
        corpus = await self._load()
        value = corpus.section(page_id, heading_path)
        if value is None:
            return {"unavailable": True}
        page = corpus.page(page_id)
        self._cite(page_id, value["heading_path"])
        self._note(f"section:{page_id}|{value['heading_path']}")
        text = value["text"]
        return value | {
            "text": text[:SECTION_LIMIT],
            "truncated": value["truncated"] or len(text) > SECTION_LIMIT,
            "current_version": page.version if page else None,
            "reviewed_version": (self.bundle.get("subject") or {}).get("version")
            if str(page_id) == str((self.bundle.get("subject") or {}).get("page_id"))
            else None,
        }

    @staticmethod
    def _trims(record):
        def turns(r):
            r["conversation"] = r.get("conversation", [])[1:]

        def notes(r):
            for stage in r.get("stages", {}).values():
                stage.pop("notes", None)

        def inventory(r):
            r["space_inventory"] = []

        def report(r):
            r["report"] = "[omitted: prompt limit]"

        def detail(r):
            for finding in r.get("findings", []):
                finding.pop("related", None)
                finding.pop("suggested_direction", None)
                finding.pop("reason", None)
                if finding.get("verification"):
                    finding["verification"].pop("reasoning", None)

        def outline(r):
            r["outline"] = r.get("outline", [])[:40]

        def findings(r):
            r["findings"] = r.get("findings", [])[: max(1, len(r["findings"]) // 2)]
            r["findings_omitted"] = True

        turn_count = len(record.get("conversation") or [])
        return (
            [turns] * turn_count
            + [notes, inventory, report, detail, outline]
            + [findings] * 6
        )
