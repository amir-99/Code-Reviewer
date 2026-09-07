import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from reviewer.services.docs.confluence import DocumentContext
from reviewer.services.forge.gitlab import MergeRequestContext
from reviewer.services.issues.jira import EpicContext, IssueContext

ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9]{1,9}-[0-9]+$"


class Linkage(BaseModel):
    issue_key: str | None = None
    resolved_from: (
        Literal["branch", "title", "description", "commit", "manual"] | None
    ) = None
    secondary_keys: list[str] = []
    warnings: list[str] = []


ReportMode = Literal["applied", "draft", "none"]


class ReviewOverrides(BaseModel):
    """Operator-supplied context for a manually triggered review.

    Every field is optional: a manual run supplying none of them behaves exactly
    like a webhook run. A supplied issue key replaces branch/title/commit
    discovery rather than adding to it, so an operator can review a change whose
    branch carries no key at all.
    """

    model_config = {"extra": "forbid"}

    issue_key: str | None = Field(default=None, pattern=ISSUE_KEY_PATTERN)
    epic_key: str | None = Field(default=None, pattern=ISSUE_KEY_PATTERN)
    document_urls: list[str] = Field(default_factory=list, max_length=20)
    requested_by: str = Field(default="", max_length=128)
    # How the finished report reaches the merge request. "applied" posts it,
    # "draft" renders and stores it without writing to GitLab, "none" publishes
    # nothing at all. Persisted with the review so a replay reuses the mode the
    # run was triggered with. None means "whatever the project config says".
    report_mode: ReportMode | None = None


class Hunk(BaseModel):
    old_start: int | None
    old_lines: int | None
    new_start: int | None
    new_lines: int | None
    header: str


class DiffLine(BaseModel):
    text: str
    old_line: int | None
    new_line: int | None
    kind: Literal["added", "removed", "context"]


class ChangedFile(BaseModel):
    path: str
    old_path: str | None = None
    change_type: Literal["added", "modified", "deleted", "renamed"]
    language: str | None = None
    hunks: list[Hunk] = []
    is_generated: bool = False
    is_vendored: bool = False
    is_excluded: bool = False
    size_bytes: int = 0
    lines: list[DiffLine] = []


class CodeContext(BaseModel):
    merge_base_sha: str
    head_sha: str
    target_branch: str
    files: list[ChangedFile]
    worktree_path: Path
    total_changed_lines: int


class StaticAnalysisResult(BaseModel):
    name: str
    required: bool
    exit_code: int | None
    stdout_tail: str
    duration_s: float
    status: Literal["passed", "failed", "errored", "skipped"]


class Budget(BaseModel):
    token_ceiling: int
    deadline_at: datetime
    model_tier: dict[str, str]
    tokens_used: int = 0


class ContextBundle(BaseModel):
    review_id: UUID
    mr: MergeRequestContext
    linkage: Linkage
    issue: IssueContext | None = None
    epic: EpicContext | None = None
    documents: list[DocumentContext] = []
    code: CodeContext
    static: list[StaticAnalysisResult] = []
    budget: Budget
    degradations: list[str] = []
    aggregated_findings: list[dict] = []
    jira_base_url: str = ""

    def content_hash(self):
        value = self.model_dump(
            mode="json",
            exclude={
                "review_id",
                "budget",
                "aggregated_findings",
                "degradations",
                "jira_base_url",
            },
        )
        value["code"].pop("worktree_path", None)
        for result in value.get("static", []):
            result.pop("duration_s", None)
        return sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
