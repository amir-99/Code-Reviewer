"""M0 contract and fake; no assumptions about the installation's API version."""

from typing import Literal, Protocol

import httpx
from pydantic import BaseModel


class DocumentRef(BaseModel):
    page_id: str
    url: str


class DocumentContext(BaseModel):
    source: Literal["confluence"] = "confluence"
    page_id: str
    space: str
    title: str
    url: str
    version: int
    text_md: str
    truncated: bool = False


class PageComment(BaseModel):
    """One comment on a page, as the content API reports it."""

    id: str
    page_id: str
    body_storage: str
    version: int
    author: str
    location: Literal["footer", "inline"] = "footer"
    resolved: bool = False
    selection: str | None = None


class SearchHit(BaseModel):
    page_id: str
    title: str
    url: str
    excerpt: str = ""
    version: int | None = None


class InlineUnsupported(RuntimeError):
    """Confluence refused the inline-comment payload; post a footer comment."""


class DocumentService(Protocol):
    async def resolve(self, url: str) -> DocumentRef | None: ...
    async def fetch(self, ref: DocumentRef) -> DocumentContext | None: ...


# CQL string literal: quotes and backslashes escaped, length bounded so a model
# supplied query cannot become an arbitrarily large request.
CQL_TERM_LIMIT = 200


def cql_string(value):
    value = str(value or "")[:CQL_TERM_LIMIT]
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class FakeDocumentService:
    def __init__(self, pages=None, user="reviewer-bot"):
        self.pages = pages or {}
        self.user = user
        self.comments_by_page = {}
        self.next_id = 1000
        self.inline_supported = True
        self.writes = []

    async def identity(self):
        return self.user

    def page_url(self, page_id):
        for url, page in self.pages.items():
            if page.page_id == str(page_id):
                return url
        return f"https://wiki.internal/pages/viewpage.action?pageId={page_id}"

    async def resolve(self, url):
        page = self.pages.get(url)
        return DocumentRef(page_id=page.page_id, url=url) if page else None

    async def fetch(self, ref):
        page = self.pages.get(ref.url)
        if page is None:
            page = next(
                (p for p in self.pages.values() if p.page_id == ref.page_id), None
            )
        return page

    async def children(self, ref):
        return []

    async def space_pages(self, space, limit=50):
        return [
            SearchHit(page_id=p.page_id, title=p.title, url=p.url, version=p.version)
            for p in self.pages.values()
            if p.space == space
        ][:limit]

    async def search(self, space, query, limit=10):
        words = {w for w in query.lower().split() if len(w) > 2}
        hits = []
        for p in self.pages.values():
            text = (p.title + "\n" + p.text_md).lower()
            if p.space == space and any(w in text for w in words):
                hits.append(
                    SearchHit(
                        page_id=p.page_id,
                        title=p.title,
                        url=p.url,
                        excerpt=p.text_md[:200],
                        version=p.version,
                    )
                )
        return hits[:limit]

    async def comments(self, page_id, location=None):
        rows = self.comments_by_page.get(str(page_id), [])
        return [c for c in rows if location is None or c.location == location]

    def _add(self, page_id, body, location, selection=None):
        self.next_id += 1
        comment = PageComment(
            id=str(self.next_id),
            page_id=str(page_id),
            body_storage=body,
            version=1,
            author=self.user,
            location=location,
            selection=selection,
        )
        self.comments_by_page.setdefault(str(page_id), []).append(comment)
        self.writes.append(("post", comment.id))
        return comment

    async def post_comment(self, page_id, body_storage):
        return self._add(page_id, body_storage, "footer")

    async def post_inline_comment(self, page_id, body_storage, selection):
        if not self.inline_supported:
            raise InlineUnsupported("inline comments unsupported")
        return self._add(page_id, body_storage, "inline", selection)

    async def edit_comment(self, comment_id, body_storage, version):
        for rows in self.comments_by_page.values():
            for i, c in enumerate(rows):
                if c.id == str(comment_id):
                    if c.version != version:
                        raise ValueError("version conflict")
                    rows[i] = c.model_copy(
                        update={"body_storage": body_storage, "version": version + 1}
                    )
                    self.writes.append(("edit", c.id))
                    return rows[i]
        raise KeyError(comment_id)

    async def delete_comment(self, comment_id):
        for rows in self.comments_by_page.values():
            for c in list(rows):
                if c.id == str(comment_id):
                    rows.remove(c)
                    self.writes.append(("delete", c.id))
                    return True
        return False

    async def close(self):
        return None


class Confluence:
    def __init__(self, base_url, token, transport=None):
        from urllib.parse import urlsplit

        self.base = base_url.rstrip("/") + "/"
        self.origin = urlsplit(self.base)
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
            transport=transport,
        )

    def allowed(self, url):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        return (
            (parsed.scheme, parsed.hostname, parsed.port)
            == (self.origin.scheme, self.origin.hostname, self.origin.port)
            and not parsed.username
            and not parsed.password
        )

    async def resolve(self, url):
        import re
        from urllib.parse import parse_qs, unquote_plus, urljoin, urlsplit

        for _ in range(5):
            if not self.allowed(url):
                return None
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            page_id = None
            if re.search(r"/pages/viewpage.action$", parts.path):
                page_id = query.get("pageId", [None])[0]
            match = re.search(r"/spaces/[^/]+/pages/(\d+)", parts.path)
            if match:
                page_id = match[1]
            if page_id and str(page_id).isdigit():
                return DocumentRef(page_id=page_id, url=url)
            match = re.search(r"/display/([^/]+)/(.+)", parts.path)
            if match:
                r = await self.client.get(
                    self.base + "rest/api/content",
                    params={
                        "spaceKey": unquote_plus(match[1]),
                        "title": unquote_plus(match[2]),
                        "expand": "version",
                    },
                )
                r.raise_for_status()
                pages = r.json().get("results", [])
                return DocumentRef(page_id=pages[0]["id"], url=url) if pages else None
            if "/x/" in parts.path:
                r = await self.client.head(url)
                if r.status_code not in {301, 302, 303, 307, 308}:
                    return None
                url = urljoin(url, r.headers.get("location", ""))
                continue
            return None
        return None

    async def fetch(self, ref):
        from reviewer.context.conversion import markdown

        if not self.allowed(ref.url) or not ref.page_id.isdigit():
            return None
        r = await self.client.get(
            self.base + f"rest/api/content/{ref.page_id}",
            params={"expand": "body.storage,version,space"},
        )
        if r.status_code in {403, 404}:
            return None
        r.raise_for_status()
        d = r.json()
        return DocumentContext(
            page_id=d["id"],
            space=d["space"]["key"],
            title=d["title"],
            url=ref.url,
            version=d["version"]["number"],
            text_md=markdown(d["body"]["storage"]["value"]),
        )

    async def identity(self):
        r = await self.client.get(self.base + "rest/api/user/current")
        r.raise_for_status()
        d = r.json()
        return str(d.get("username") or d.get("userKey") or "")

    def page_url(self, page_id):
        return self.base + "pages/viewpage.action?pageId=" + str(page_id)

    async def space_pages(self, space, limit=50):
        """Titles of the pages in a space; bodies are fetched only on demand."""
        from urllib.parse import quote

        r = await self.client.get(
            self.base + f"rest/api/space/{quote(str(space), safe='')}/content/page",
            params={"limit": min(int(limit), 200), "expand": "version"},
        )
        if r.status_code in {403, 404}:
            return []
        r.raise_for_status()
        return [self._hit(x) for x in r.json().get("results", [])]

    async def search(self, space, query, limit=10):
        """CQL full-text search restricted to one space.

        The space key and the query are both passed as escaped CQL string
        literals: a model-supplied query can never change the shape of the
        statement, only the text it searches for.
        """
        cql = (
            f"space = {cql_string(space)} AND type = page "
            f"AND text ~ {cql_string(query)}"
        )
        r = await self.client.get(
            self.base + "rest/api/content/search",
            params={
                "cql": cql,
                "limit": min(int(limit), 50),
                "expand": "version",
                "excerpt": "highlight",
            },
        )
        if r.status_code in {400, 403, 404}:
            return []
        r.raise_for_status()
        return [self._hit(x) for x in r.json().get("results", [])]

    def _hit(self, data):
        import re

        content = data.get("content") or data
        excerpt = data.get("excerpt") or ""
        return SearchHit(
            page_id=str(content["id"]),
            title=content.get("title") or data.get("title") or "",
            url=self.page_url(content["id"]),
            excerpt=re.sub(r"@@@(end)?hl@@@", "", excerpt)[:400],
            version=((content.get("version") or {}).get("number")),
        )

    async def comments(self, page_id, location=None):
        """Every comment on a page, footer and inline, replies included."""
        params = {
            "depth": "all",
            "limit": 200,
            "expand": "body.storage,version,history,extensions.inlineProperties,"
            "extensions.resolution",
        }
        if location:
            params["location"] = location
        r = await self.client.get(
            self.base + f"rest/api/content/{int(page_id)}/child/comment",
            params=params,
        )
        if r.status_code in {403, 404}:
            return []
        r.raise_for_status()
        return [self._comment(x, str(page_id)) for x in r.json().get("results", [])]

    @staticmethod
    def _comment(data, page_id):
        extensions = data.get("extensions") or {}
        inline = extensions.get("inlineProperties") or {}
        resolution = extensions.get("resolution") or {}
        author = (
            (data.get("history") or {}).get("createdBy")
            or (data.get("version") or {}).get("by")
            or {}
        )
        return PageComment(
            id=str(data["id"]),
            page_id=page_id,
            body_storage=((data.get("body") or {}).get("storage") or {}).get(
                "value", ""
            ),
            version=(data.get("version") or {}).get("number", 1),
            author=str(author.get("username") or author.get("userKey") or ""),
            location="inline" if extensions.get("location") == "inline" else "footer",
            resolved=resolution.get("status") == "resolved",
            selection=inline.get("originalSelection"),
        )

    async def post_comment(self, page_id, body_storage):
        return await self._post(page_id, body_storage, None)

    async def post_inline_comment(self, page_id, body_storage, selection):
        """Best effort: the Data Center payload is undocumented and version
        dependent, so any client error is reported as unsupported and the caller
        falls back to a footer comment."""
        import json
        import time

        extensions = {
            "location": "inline",
            "inlineProperties": {
                "originalSelection": selection,
                "numMatches": 1,
                "matchIndex": 0,
                "serializedHighlights": json.dumps([[selection]]),
                "lastFetchTime": str(int(time.time() * 1000)),
            },
        }
        try:
            return await self._post(page_id, body_storage, extensions)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise InlineUnsupported(str(exc.response.status_code)) from None
            raise

    async def _post(self, page_id, body_storage, extensions):
        payload = {
            "type": "comment",
            "container": {"id": str(int(page_id)), "type": "page"},
            "body": {"storage": {"value": body_storage, "representation": "storage"}},
        }
        if extensions:
            payload["extensions"] = extensions
        r = await self.client.post(
            self.base + "rest/api/content",
            json=payload,
            params={"expand": "body.storage,version,history,extensions"},
        )
        r.raise_for_status()
        return self._comment(r.json(), str(page_id))

    async def edit_comment(self, comment_id, body_storage, version):
        r = await self.client.put(
            self.base + f"rest/api/content/{int(comment_id)}",
            json={
                "type": "comment",
                "version": {"number": int(version) + 1},
                "body": {
                    "storage": {"value": body_storage, "representation": "storage"}
                },
            },
            params={"expand": "body.storage,version,history,extensions"},
        )
        r.raise_for_status()
        data = r.json()
        return self._comment(data, str(((data.get("container") or {}).get("id")) or ""))

    async def delete_comment(self, comment_id):
        r = await self.client.delete(self.base + f"rest/api/content/{int(comment_id)}")
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    async def children(self, ref):
        if not self.allowed(ref.url):
            return []
        r = await self.client.get(
            self.base + f"rest/api/content/{ref.page_id}/child/page",
            params={"limit": 50},
        )
        r.raise_for_status()
        return [
            DocumentRef(
                page_id=x["id"],
                url=self.base + "pages/viewpage.action?pageId=" + x["id"],
            )
            for x in r.json()["results"]
        ]

    async def close(self):
        await self.client.aclose()
