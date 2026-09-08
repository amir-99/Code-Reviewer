from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field


class MergeRequestContext(BaseModel):
    project_id: int
    iid: int
    head_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    source_branch: str = ""
    target_branch: str = ""
    title: str = ""
    description: str = ""
    state: str = "opened"
    draft: bool = False
    repository_url: str = ""
    web_url: str = ""


class ForgeService(Protocol):
    async def get_diff_refs(self, project_id: int, iid: int) -> "DiffRefs": ...
    async def get_changed_paths(self, project_id: int, iid: int) -> list[str]: ...
    async def list_discussions(
        self, project_id: int, iid: int
    ) -> list["Discussion"]: ...
    async def post_inline_discussion(
        self, project_id: int, iid: int, body: str, position: "Position"
    ) -> "Discussion": ...
    async def post_note(self, project_id: int, iid: int, body: str) -> "Note": ...
    async def list_draft_notes(
        self, project_id: int, iid: int
    ) -> list["DraftNote"]: ...
    async def post_draft_note(
        self,
        project_id: int,
        iid: int,
        body: str,
        position: "Position | None" = None,
        in_reply_to_discussion_id: str | None = None,
        resolve_discussion: bool = False,
    ) -> "DraftNote": ...
    async def resolve_discussion(
        self, project_id: int, iid: int, discussion_id: str
    ) -> None: ...
    async def get_merge_request(
        self, project_id: int, iid: int
    ) -> MergeRequestContext: ...
    async def project_id_for_path(self, path: str) -> int | None: ...
    async def set_commit_status(
        self,
        project_id: int,
        sha: str,
        state: Literal["pending", "success", "failed"],
        name: str,
        description: str,
        target_url: str,
    ) -> None: ...


class GitLab:
    def __init__(self, base_url: str, token: str, transport=None):
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v4/",
            headers={"PRIVATE-TOKEN": token},
            timeout=10,
            follow_redirects=False,
            transport=transport,
        )

    async def get_merge_request(self, project_id: int, iid: int) -> MergeRequestContext:
        response = await self.client.get(f"projects/{project_id}/merge_requests/{iid}")
        response.raise_for_status()
        value = response.json()
        return MergeRequestContext(
            project_id=project_id,
            iid=iid,
            head_sha=value["sha"],
            **{
                key: value.get(key, default)
                for key, default in {
                    "source_branch": "",
                    "target_branch": "",
                    "title": "",
                    "description": "",
                    "state": "opened",
                    "draft": False,
                }.items()
            },
        )

    async def project_id_for_path(self, path: str) -> int | None:
        """Numeric id for a namespace/project path taken from a web URL."""
        from urllib.parse import quote

        response = await self.client.get(f"projects/{quote(path, safe='')}")
        if response.status_code in {403, 404}:
            return None
        response.raise_for_status()
        return int(response.json()["id"])

    async def set_commit_status(
        self, project_id, sha, state, name, description, target_url
    ):
        response = await self.client.post(
            f"projects/{project_id}/statuses/{sha}",
            json={
                "state": state,
                "name": name,
                "description": description,
                **({"target_url": target_url} if target_url else {}),
            },
        )
        response.raise_for_status()

    async def close(self):
        await self.client.aclose()

    async def identity(self):
        if not hasattr(self, "bot_id"):
            r = await self.client.get("user")
            r.raise_for_status()
            self.bot_id = r.json()["id"]
        return self.bot_id

    async def get_diff_refs(self, project_id, iid):
        r = await self.client.get(f"projects/{project_id}/merge_requests/{iid}")
        r.raise_for_status()
        return DiffRefs.model_validate(r.json()["diff_refs"])

    async def get_changed_paths(self, project_id, iid):
        paths = []
        page = 1
        while True:
            r = await self.client.get(
                f"projects/{project_id}/merge_requests/{iid}/diffs",
                params={"page": page, "per_page": 100},
            )
            r.raise_for_status()
            data = r.json()
            paths.extend(x["new_path"] for x in data)
            if len(data) < 100:
                return paths
            page += 1

    async def repository_url(self, project_id):
        r = await self.client.get(f"projects/{project_id}")
        r.raise_for_status()
        return r.json()["http_url_to_repo"]

    async def list_discussions(self, project_id, iid):
        output = []
        page = 1
        while True:
            r = await self.client.get(
                f"projects/{project_id}/merge_requests/{iid}/discussions",
                params={"page": page, "per_page": 100},
            )
            r.raise_for_status()
            data = r.json()
            output.extend(
                Discussion(
                    id=d["id"],
                    file=next(
                        (
                            n.get("position", {}).get("new_path")
                            for n in d["notes"]
                            if n.get("position")
                        ),
                        None,
                    ),
                    resolved=all(n.get("resolved", False) for n in d["notes"]),
                    notes=[
                        Note(id=n["id"], body=n["body"], author_id=n["author"]["id"])
                        for n in d["notes"]
                    ],
                )
                for d in data
            )
            if len(data) < 100:
                return output
            page += 1

    async def post_inline_discussion(self, project_id, iid, body, position):
        r = await self.client.post(
            f"projects/{project_id}/merge_requests/{iid}/discussions",
            json={"body": body, "position": position.model_dump(exclude_none=True)},
        )
        if r.status_code == 400:
            raise PositionError("Anchor not positionable")
        r.raise_for_status()
        d = r.json()
        return Discussion(
            id=d["id"],
            notes=[
                Note(id=n["id"], body=n["body"], author_id=n["author"]["id"])
                for n in d["notes"]
            ],
        )

    async def post_note(self, project_id, iid, body):
        r = await self.client.post(
            f"projects/{project_id}/merge_requests/{iid}/notes", json={"body": body}
        )
        r.raise_for_status()
        d = r.json()
        return Note(id=d["id"], body=d["body"], author_id=d["author"]["id"])

    async def list_draft_notes(self, project_id, iid):
        """The reviewer's own pending draft notes on a merge request.

        GitLab scopes draft notes to their author, so this is what the bot
        already drafted and nobody else has seen.
        """
        output = []
        page = 1
        while True:
            r = await self.client.get(
                f"projects/{project_id}/merge_requests/{iid}/draft_notes",
                params={"page": page, "per_page": 100},
            )
            r.raise_for_status()
            data = r.json()
            output.extend(draft_note(d) for d in data)
            if len(data) < 100:
                return output
            page += 1

    async def post_draft_note(
        self,
        project_id,
        iid,
        body,
        position=None,
        in_reply_to_discussion_id=None,
        resolve_discussion=False,
    ):
        """Create a pending comment instead of posting it.

        A draft note stays invisible to the merge request until it is published,
        so a drafted run leaves the review where an operator can see it in
        GitLab without notifying anyone or resolving anything.
        """
        payload = {"note": body}
        if position is not None:
            payload["position"] = position.model_dump(exclude_none=True)
        if in_reply_to_discussion_id is not None:
            payload["in_reply_to_discussion_id"] = in_reply_to_discussion_id
        if resolve_discussion:
            payload["resolve_discussion"] = True
        r = await self.client.post(
            f"projects/{project_id}/merge_requests/{iid}/draft_notes", json=payload
        )
        if r.status_code == 400 and position is not None:
            raise PositionError("Anchor not positionable")
        r.raise_for_status()
        return draft_note(r.json())

    async def reply(self, project_id, iid, discussion_id, body):
        r = await self.client.post(
            f"projects/{project_id}/merge_requests/{iid}/discussions/{discussion_id}/notes",
            json={"body": body},
        )
        r.raise_for_status()

    async def resolve_discussion(self, project_id, iid, discussion_id):
        r = await self.client.put(
            f"projects/{project_id}/merge_requests/{iid}/discussions/{discussion_id}",
            json={"resolved": True},
        )
        r.raise_for_status()

    async def role(self, project_id, user_id):
        r = await self.client.get(f"projects/{project_id}/members/all/{user_id}")
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return r.json()["access_level"]


class FakeForge:
    def __init__(self, mr: MergeRequestContext):
        self.mr = mr
        self.statuses = []
        self.comments = []
        self.error = None
        self.paths = []
        self.discussions = []
        self.bot_id = 900
        self.bad_position = False
        self.roles = {}
        self.replies = []
        self.projects = {}
        self.draft_notes = []

    async def get_merge_request(self, project_id, iid):
        if self.error:
            raise self.error
        return self.mr.model_copy()

    async def project_id_for_path(self, path):
        return self.projects.get(path)

    async def set_commit_status(
        self, project_id, sha, state, name, description, target_url
    ):
        self.statuses.append(
            {"project_id": project_id, "sha": sha, "state": state, "name": name}
        )

    async def identity(self):
        return self.bot_id

    async def get_diff_refs(self, project_id, iid):
        return DiffRefs(
            base_sha="b" * 40, start_sha="b" * 40, head_sha=self.mr.head_sha
        )

    async def get_changed_paths(self, project_id, iid):
        return self.paths

    async def repository_url(self, project_id):
        return self.mr.repository_url

    async def list_discussions(self, project_id, iid):
        return self.discussions

    async def post_inline_discussion(self, project_id, iid, body, position):
        if self.bad_position:
            raise PositionError()
        note = Note(id=len(self.comments) + 1, body=body, author_id=self.bot_id)
        self.comments.append(note)
        d = Discussion(id=str(note.id), notes=[note], file=position.new_path)
        self.discussions.append(d)
        return d

    async def post_note(self, project_id, iid, body):
        note = Note(id=len(self.comments) + 1, body=body, author_id=self.bot_id)
        self.comments.append(note)
        self.discussions.append(Discussion(id=str(note.id), notes=[note]))
        return note

    async def list_draft_notes(self, project_id, iid):
        return list(self.draft_notes)

    async def post_draft_note(
        self,
        project_id,
        iid,
        body,
        position=None,
        in_reply_to_discussion_id=None,
        resolve_discussion=False,
    ):
        if self.bad_position and position is not None:
            raise PositionError()
        note = DraftNote(
            id=len(self.draft_notes) + 1,
            body=body,
            author_id=self.bot_id,
            file=position.new_path if position else None,
            discussion_id=in_reply_to_discussion_id,
            resolve_discussion=resolve_discussion,
        )
        self.draft_notes.append(note)
        return note

    async def reply(self, project_id, iid, discussion_id, body):
        self.replies.append((discussion_id, body))
        note = Note(id=len(self.comments) + 1, body=body, author_id=self.bot_id)
        self.comments.append(note)
        for discussion in self.discussions:
            if discussion.id == discussion_id:
                discussion.notes.append(note)
        return note

    async def resolve_discussion(self, project_id, iid, discussion_id):
        next(d for d in self.discussions if d.id == discussion_id).resolved = True

    async def role(self, project_id, user_id):
        return self.roles.get(user_id, 0)


class DiffRefs(BaseModel):
    base_sha: str
    start_sha: str
    head_sha: str


class Position(DiffRefs):
    position_type: Literal["text"] = "text"
    old_path: str
    new_path: str
    new_line: int | None = None
    old_line: int | None = None


class Note(BaseModel):
    id: int
    body: str
    author_id: int = 0


class DraftNote(BaseModel):
    """A comment pending publication, visible only to the account that wrote it."""

    id: int
    body: str = ""
    author_id: int = 0
    file: str | None = None
    discussion_id: str | None = None
    resolve_discussion: bool = False


def draft_note(payload):
    position = payload.get("position") or {}
    return DraftNote(
        id=payload["id"],
        body=payload.get("note") or "",
        author_id=payload.get("author_id") or 0,
        file=position.get("new_path") or position.get("old_path"),
        discussion_id=payload.get("discussion_id"),
        resolve_discussion=bool(payload.get("resolve_discussion")),
    )


class Discussion(BaseModel):
    id: str
    notes: list[Note] = []
    resolved: bool = False
    file: str | None = None


class PositionError(RuntimeError):
    pass


class StaleReview(RuntimeError):
    pass
