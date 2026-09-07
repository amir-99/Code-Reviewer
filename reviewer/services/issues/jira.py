"""M0 contract and fake. Live requirement collection awaits PRD §14 answers."""

from typing import Protocol

from pydantic import BaseModel, Field


class AcceptanceCriterion(BaseModel):
    id: str
    text: str


class IssueContext(BaseModel):
    key: str
    type: str
    summary: str
    description_md: str
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    status: str
    components: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class EpicContext(BaseModel):
    key: str
    summary: str
    description_md: str


class IssueService(Protocol):
    async def get_issue(self, key: str) -> IssueContext | None: ...
    async def get_epic_for(self, key: str) -> EpicContext | None: ...
    async def get_document_links(self, key: str) -> list[str]: ...


class FakeIssueService:
    def __init__(self, issues=None, epics=None, links=None):
        self.issues = issues or {}
        self.epics = epics or {}
        self.links = links or {}

    async def get_issue(self, key):
        return self.issues.get(key)

    async def get_epic_for(self, key):
        return self.epics.get(key)

    async def get_document_links(self, key):
        return list(self.links.get(key, []))


class Jira:
    def __init__(
        self, base_url, token, ac_heading="Acceptance Criteria", transport=None
    ):
        import httpx

        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
            follow_redirects=False,
            transport=transport,
        )
        self.heading = ac_heading
        self.epic_field = None
        self.field_time = 0
        self.raw = {}

    async def discover(self):
        import time

        if self.field_time and time.monotonic() - self.field_time < 86400:
            return
        r = await self.client.get("rest/api/2/field")
        r.raise_for_status()
        self.epic_field = next(
            (
                x["id"]
                for x in r.json()
                if x.get("schema", {}).get("custom")
                == "com.pyxis.greenhopper.jira:gh-epic-link"
            ),
            None,
        )
        self.field_time = time.monotonic()

    async def _issue(self, key):
        from urllib.parse import quote

        r = await self.client.get(
            f"rest/api/2/issue/{quote(key, safe='')}",
            params={"expand": "renderedFields"},
        )
        if r.status_code in {403, 404}:
            return None
        r.raise_for_status()
        self.raw[key] = r.json()
        return r.json()

    async def get_issue(self, key):
        from reviewer.context.conversion import criteria, jira_wiki, markdown

        data = await self._issue(key)
        if not data:
            return None
        fields = data["fields"]
        rendered = data.get("renderedFields", {}).get("description")
        description = (
            markdown(rendered)
            if rendered
            else jira_wiki(fields.get("description") or "")
        )
        return IssueContext(
            key=key,
            type=fields["issuetype"]["name"],
            summary=fields["summary"],
            description_md=description,
            acceptance_criteria=criteria(description, self.heading),
            status=fields["status"]["name"],
            components=[x["name"] for x in fields.get("components", [])],
            labels=fields.get("labels", []),
        )

    async def get_epic_for(self, key):
        await self.discover()
        data = self.raw.get(key) or await self._issue(key)
        if not data:
            return None
        fields = data["fields"]
        epic_key = fields.get(self.epic_field) if self.epic_field else None
        parent = fields.get("parent", {})
        if (
            not epic_key
            and parent.get("fields", {}).get("issuetype", {}).get("name", "").lower()
            == "epic"
        ):
            epic_key = parent.get("key")
        if not epic_key:
            return None
        issue = await self.get_issue(epic_key)
        return (
            EpicContext(
                key=issue.key,
                summary=issue.summary,
                description_md=issue.description_md,
            )
            if issue
            else None
        )

    async def get_document_links(self, key):
        import re
        from urllib.parse import quote

        r = await self.client.get(f"rest/api/2/issue/{quote(key, safe='')}/remotelink")
        r.raise_for_status()
        links = [x.get("object", {}).get("url", "") for x in r.json()]
        fields = self.raw.get(key, {}).get("fields", {})
        links += re.findall(r'https?://[^\s<>\]\)"|]+', fields.get("description") or "")
        return list(dict.fromkeys(x for x in links if x))

    async def updated(self, key):
        data = await self._issue(key)
        return data["fields"].get("updated") if data else None

    async def comments(self, key):
        from urllib.parse import quote

        from reviewer.context.conversion import jira_wiki

        r = await self.client.get(
            f"rest/api/2/issue/{quote(key, safe='')}/comment", params={"maxResults": 10}
        )
        r.raise_for_status()
        return "\n".join(
            jira_wiki(c.get("body", "")) for c in r.json().get("comments", [])
        )[:10000]

    async def close(self):
        await self.client.aclose()
