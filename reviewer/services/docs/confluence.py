"""M0 contract and fake; no assumptions about the installation's API version."""

from typing import Literal, Protocol

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


class DocumentService(Protocol):
    async def resolve(self, url: str) -> DocumentRef | None: ...
    async def fetch(self, ref: DocumentRef) -> DocumentContext | None: ...


class FakeDocumentService:
    def __init__(self, pages=None):
        self.pages = pages or {}

    async def resolve(self, url):
        page = self.pages.get(url)
        return DocumentRef(page_id=page.page_id, url=url) if page else None

    async def fetch(self, ref):
        return self.pages.get(ref.url)


class Confluence:
    def __init__(self, base_url, token, transport=None):
        from urllib.parse import urlsplit

        import httpx

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
