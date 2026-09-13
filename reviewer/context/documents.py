"""The pages a document review reads, fetched once and searched locally.

The reviewed page is the subject; supporting pages, children and pages found
in the same space are reference material. Every body is secret-scanned and
redacted before a model sees a byte of it, and everything a model may later
ask for — a section, a page outline, a search — is answered from this local
copy, never by browsing.
"""

import re
from collections import Counter

from pydantic import BaseModel

from reviewer.context.models import ChangedFile, DiffLine
from reviewer.context.redaction import Redactor
from reviewer.services.docs.confluence import DocumentRef

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
INTRO = "(intro)"
CHARS_PER_TOKEN = 4
SEARCH_LIMIT = 8
EXCERPT_CHARS = 400
# Words shorter than this carry no signal for a title-seeded space search.
SEED_WORD_MIN = 4
SEED_WORDS = 8
STOPWORDS = frozenset(
    "this that with from have will your they their there which about into than "
    "then them these those were been being also each other some such only over "
    "under before after between should would could must when where while".split()
)


class DocumentSection(BaseModel):
    page_id: str
    heading_path: str
    ordinal: int
    text: str
    truncated: bool = False

    @property
    def id(self):
        return f"{self.page_id}#{self.ordinal}"


class CorpusPage(BaseModel):
    page_id: str
    space: str
    title: str
    url: str
    version: int
    role: str  # subject | supporting | child | space
    text: str
    truncated: bool = False


def split_sections(page_id, text):
    """One section per heading; text before the first heading is `(intro)`."""
    sections, path, buffer = [], [], []
    heading_path = INTRO

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            sections.append(
                DocumentSection(
                    page_id=page_id,
                    heading_path=heading_path,
                    ordinal=len(sections),
                    text=body,
                )
            )
        buffer.clear()

    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            level, title = len(match[1]), match[2].strip() or "(untitled)"
            path = path[: level - 1] + [title]
            heading_path = " > ".join(path)
            buffer.append(line)
        else:
            buffer.append(line)
    flush()
    return sections


def words(text):
    return {
        w for w in re.findall(r"[a-z0-9_-]+", text.lower()) if len(w) >= SEED_WORD_MIN
    }


class DocumentCorpus:
    def __init__(self, subject, redactor=None):
        self.subject = dict(subject)
        self.subject_page_id = str(subject["page_id"])
        self.redactor = redactor or Redactor()
        self.pages = {}
        self.sections = {}
        self.inventory = []
        self.degradations = []
        self.comments = {}

    # -- collection ---------------------------------------------------------

    async def collect(self, docs, overrides, config, scanner):
        """Fetch the subject, supporting pages, children and related pages.

        Order matters for the page cap: the subject always fits, then the pages
        the requester named, then children, then whatever the space search
        found. Overflow is recorded, not silently dropped.
        """
        subject_ref = DocumentRef(
            page_id=self.subject_page_id, url=self.subject.get("url", "")
        )
        page = await docs.fetch(subject_ref)
        if page is None:
            raise LookupError("The reviewed page is no longer readable")
        if int(page.version) != int(self.subject.get("version", page.version)):
            raise LookupError("The page changed since the review was admitted")
        await self._add(page, "subject", scanner)
        limit = config.documents.max_pages
        for url in list(dict.fromkeys(getattr(overrides, "supporting_urls", []) or [])):
            if len(self.pages) >= limit:
                self._degrade("docs_page_cap")
                break
            try:
                ref = await docs.resolve(url)
                extra = await docs.fetch(ref) if ref else None
            except Exception:
                extra = None
            if extra is None:
                self._degrade("docs_unavailable")
                continue
            await self._add(extra, "supporting", scanner)
        if config.documents.child_depth > 0 and hasattr(docs, "children"):
            frontier = [(subject_ref, 0)]
            while frontier and len(self.pages) < limit:
                ref, depth = frontier.pop(0)
                if depth >= config.documents.child_depth:
                    continue
                try:
                    children = await docs.children(ref)
                except Exception:
                    children = []
                for child in children:
                    if len(self.pages) >= limit:
                        self._degrade("docs_page_cap")
                        break
                    extra = await docs.fetch(child)
                    if extra is not None and str(extra.page_id) not in self.pages:
                        await self._add(extra, "child", scanner)
                        frontier.append((child, depth + 1))
        if getattr(overrides, "check_space", False):
            await self._collect_space(docs, page, config, scanner)
        self._apply_token_cap(config.documents.max_tokens * CHARS_PER_TOKEN)
        return self

    async def _collect_space(self, docs, page, config, scanner):
        try:
            self.inventory = [
                {"page_id": h.page_id, "title": h.title, "url": h.url}
                for h in await docs.space_pages(
                    page.space, limit=config.document_review.space_pages
                )
                if h.page_id != self.subject_page_id
            ]
        except Exception:
            self._degrade("space_unavailable")
            return
        # Title and heading words first, then the body's most frequent words, so
        # a page that shares vocabulary but not headings is still found.
        seeds = sorted(words(page.title))
        for section in split_sections(page.page_id, page.text_md):
            if section.heading_path != INTRO:
                seeds += sorted(words(section.heading_path.split(" > ")[-1]))
        counts = Counter(
            w
            for w in re.findall(r"[a-z][a-z0-9_-]+", page.text_md.lower())
            if len(w) >= SEED_WORD_MIN and w not in STOPWORDS
        )
        seeds += [w for w, _ in counts.most_common(SEED_WORDS)]
        query = " ".join(list(dict.fromkeys(seeds))[: SEED_WORDS * 2])
        if not query:
            return
        try:
            hits = await docs.search(page.space, query, limit=SEARCH_LIMIT * 2)
        except Exception:
            self._degrade("space_unavailable")
            return
        fetched = 0
        for hit in hits:
            if fetched >= config.document_review.space_related:
                break
            if (
                hit.page_id in self.pages
                or len(self.pages) >= config.documents.max_pages
            ):
                continue
            try:
                extra = await docs.fetch(DocumentRef(page_id=hit.page_id, url=hit.url))
            except Exception:
                extra = None
            if extra is not None:
                await self._add(extra, "space", scanner)
                fetched += 1

    async def _add(self, page, role, scanner):
        text = page.text_md
        if scanner is not None:
            try:
                matches = await scanner.scan(
                    [
                        ChangedFile(
                            path=f"page-{page.page_id}.md",
                            change_type="modified",
                            lines=[
                                DiffLine(
                                    text=line, new_line=n, old_line=n, kind="context"
                                )
                                for n, line in enumerate(text.splitlines(), 1)
                            ],
                        )
                    ]
                )
            except Exception:
                matches = []
                self._degrade("secret_scan_unavailable")
            self.redactor.secrets.update(Redactor(matches).secrets)
        text = self.redactor.text(text)
        page_id = str(page.page_id)
        self.pages[page_id] = CorpusPage(
            page_id=page_id,
            space=page.space,
            title=page.title,
            url=page.url,
            version=page.version,
            role=role,
            text=text,
            truncated=page.truncated,
        )
        self.sections[page_id] = split_sections(page_id, text)

    def _apply_token_cap(self, budget_chars):
        """Reference pages shrink first, section by section, the subject last."""
        total = sum(len(s.text) for ss in self.sections.values() for s in ss)
        if total <= budget_chars:
            return
        self._degrade("docs_truncated")
        order = sorted(
            self.pages.values(),
            key=lambda p: ("subject", "supporting", "child", "space").index(p.role),
            reverse=True,
        )
        for page in order:
            for section in reversed(self.sections[page.page_id]):
                if total <= budget_chars:
                    return
                keep = max(200, len(section.text) - (total - budget_chars))
                if keep < len(section.text):
                    total -= len(section.text) - keep
                    section.text = section.text[:keep]
                    section.truncated = True
                    page.truncated = True

    def _degrade(self, name):
        if name not in self.degradations:
            self.degradations.append(name)

    # -- what the model can ask for -----------------------------------------

    def sections_for(self, page_id):
        return self.sections.get(str(page_id), [])

    def page(self, page_id):
        return self.pages.get(str(page_id))

    def outline(self, page_id):
        page = self.page(page_id)
        if page is None:
            return None
        return {
            "page_id": page.page_id,
            "title": page.title,
            "role": page.role,
            "version": page.version,
            "truncated": page.truncated,
            "sections": [
                {"heading_path": s.heading_path, "chars": len(s.text)}
                for s in self.sections_for(page_id)
            ],
        }

    def section(self, page_id, heading_path):
        target = re.sub(r"\s+", " ", heading_path or "").strip().lower()
        for s in self.sections_for(page_id):
            if re.sub(r"\s+", " ", s.heading_path).lower() == target:
                return {
                    "page_id": s.page_id,
                    "heading_path": s.heading_path,
                    "text": s.text,
                    "truncated": s.truncated,
                }
        # A trailing heading is enough when the full path is not repeated.
        for s in self.sections_for(page_id):
            if s.heading_path.lower().endswith(target):
                return {
                    "page_id": s.page_id,
                    "heading_path": s.heading_path,
                    "text": s.text,
                    "truncated": s.truncated,
                }
        return None

    def search(self, query, page_id=None, limit=SEARCH_LIMIT):
        """Keyword search over the local corpus; ranked by matched terms."""
        terms = [t for t in re.findall(r"[\w-]+", (query or "").lower()) if len(t) > 2]
        if not terms:
            return []
        pages = [self.page(page_id)] if page_id else list(self.pages.values())
        hits = []
        for page in pages:
            if page is None:
                continue
            for s in self.sections_for(page.page_id):
                low = s.text.lower()
                matched = [t for t in terms if t in low]
                if not matched:
                    continue
                first = min(low.find(t) for t in matched)
                start = max(0, first - EXCERPT_CHARS // 4)
                hits.append(
                    (
                        len(matched),
                        {
                            "page_id": page.page_id,
                            "title": page.title,
                            "role": page.role,
                            "heading_path": s.heading_path,
                            "excerpt": s.text[start : start + EXCERPT_CHARS],
                        },
                    )
                )
        hits.sort(key=lambda h: -h[0])
        return [h for _, h in hits[:limit]]

    def references(self):
        return [
            {
                "page_id": p.page_id,
                "title": p.title,
                "role": p.role,
                "version": p.version,
                "sections": [s.heading_path for s in self.sections_for(p.page_id)],
            }
            for p in self.pages.values()
            if p.role != "subject"
        ]

    # -- units for the reviewer ------------------------------------------------

    def units(self, unit_tokens):
        """Groups of consecutive subject sections, each within the unit budget."""
        budget = unit_tokens * CHARS_PER_TOKEN
        groups, current, size = [], [], 0
        for section in self.sections_for(self.subject_page_id):
            if current and size + len(section.text) > budget:
                groups.append(current)
                current, size = [], 0
            current.append(section)
            size += len(section.text)
        if current:
            groups.append(current)
        return [
            {"id": f"unit-{n}", "sections": group} for n, group in enumerate(groups, 1)
        ]

    def record(self):
        """What the snapshot keeps: identities and shapes, never bodies."""
        return {
            "kind": "document",
            "subject": self.subject,
            "documents": [
                {
                    "page_id": p.page_id,
                    "space": p.space,
                    "title": p.title,
                    "url": p.url,
                    "version": p.version,
                    "role": p.role,
                    "truncated": p.truncated,
                    "chars": sum(len(s.text) for s in self.sections_for(p.page_id)),
                }
                for p in self.pages.values()
            ],
            "sections": [
                {"id": s.id, "heading_path": s.heading_path, "chars": len(s.text)}
                for s in self.sections_for(self.subject_page_id)
            ],
            "space": {"inventory": self.inventory},
            "degradations": list(self.degradations),
        }
