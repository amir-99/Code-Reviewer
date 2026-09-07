from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueTrackerConfig(Strict):
    project_keys: list[str] = []
    ac_heading: str = "Acceptance Criteria"
    include_comments: bool = False
    multi_issue: bool = False
    unlinked_severity: Literal["SUGGESTION", "QUESTION", "FYI"] = "SUGGESTION"


class DocumentsConfig(Strict):
    max_pages: int = Field(default=10, ge=1, le=50)
    max_tokens: int = Field(default=30000, ge=100)
    child_depth: int = Field(default=0, ge=0, le=3)


class ReviewConfig(Strict):
    max_changed_lines: int = 3000
    token_ceiling: int = Field(default=120000, ge=1)
    timeout_s: int = Field(default=1200, ge=1, le=1200)
    unit_tokens: int = Field(default=6000, ge=256, le=20000)
    context_lines: int = 20
    max_inline: int = Field(default=15, ge=0, le=15)
    max_per_file: int = Field(default=5, ge=0, le=5)
    merge_distance: int = Field(default=10, ge=0, le=100)
    full_rereview_merge_base_delta: int = 50
    exclude: list[str] = [
        "**/node_modules/**",
        "**/vendor/**",
        "*.min.js",
        "*lock*",
        "**/*.generated.*",
    ]


class StaticTool(Strict):
    name: str
    image: str
    command: list[str]
    required: bool = False
    categories: list[str] = []
    timeout_s: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def pinned(self):
        if "@sha256:" not in self.image or not self.command:
            raise ValueError("Static tools require a digest-pinned image and command")
        return self


class ProjectConfig(Strict):
    enforcement: Literal["silent", "advisory", "gating"] = "advisory"
    milestone: Literal["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"] = (
        "M9"
    )
    issue_tracker: IssueTrackerConfig = Field(default_factory=IssueTrackerConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    static_tools: list[StaticTool] = []
    languages: list[Literal["python", "typescript", "go"]] = [
        "python",
        "typescript",
        "go",
    ]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://reviewer:local@postgres/reviewer"
    )
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    gitlab_base_url: str = "https://gitlab.example.invalid"
    gitlab_token: SecretStr = SecretStr("")
    git_read_token: SecretStr = SecretStr("")
    webhook_secrets: dict[int, SecretStr] = Field(default_factory=dict)
    admin_token: SecretStr = SecretStr("")
    milestone: Literal["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"] = (
        "M9"
    )
    jira_base_url: str = ""
    jira_token: SecretStr = SecretStr("")
    confluence_base_url: str = ""
    confluence_token: SecretStr = SecretStr("")
    gateway_base_url: str = ""
    gateway_key: SecretStr = SecretStr("")
    model_strong: str = ""
    model_fast: str = ""
    model_verifier: str = ""
    model_context_tokens: int = 32000
    classification_header: str = "X-Data-Classification"
    classification: str = "internal-source-code"
    repo_cache: Path = Path("/tmp/reviewer-repos")
    audit_path: Path = Path("/tmp/reviewer-audit")
    config_path: Path = Path("/app/config/projects.json")
    audit_retention_days: int = 30
    static_enabled: bool = False
    static_workspace_root: Path = Path("/tmp/reviewer-repos")
    otlp_endpoint: str = ""

    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"
