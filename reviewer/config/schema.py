from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# One role per model-selecting call site. Stage roles are named after the stage
# that makes the call; verification and recheck are separate roles because they
# are separate jobs — the verifier gates a blocker, the judge answers a thread —
# and an operator must be able to price and tune them independently.
ROLES = (
    "defect_review",
    "purpose",
    "design",
    "correctness",
    "complexity",
    "tests_",
    "line_review",
    "system_context",
    "verification",
    "recheck",
)

# Roles the code asked for before selection was per-role. Retained so a gateway
# configured only with MODEL_STRONG/MODEL_FAST/MODEL_VERIFIER keeps working.
LEGACY_TIERS = {
    "strong": "model_strong",
    "fast": "model_fast",
    "verification": "model_verifier",
}

# The shipped assignment, applied to any role the operator has not configured.
# Operator-supplied IDs: change them in projects.json or MODEL_ROLES, not here.
DEFAULT_ROLE_MODELS = {
    "defect_review": "google/gemini-3.8-flash",
    "purpose": "google/gemini-3.8-flash",
    "design": "google/gemini-3.8-flash",
    "correctness": "google/gemini-3.8-flash",
    "complexity": "google/gemini-3.8-flash",
    "tests_": "google/gemini-3.8-flash",
    "line_review": "openai/gpt-5.6-terra",
    "system_context": "google/gemini-3.8-flash",
    "verification": "anthropic/claude-sonnet-5",
    "recheck": "google/gemini-3.8-flash",
}

MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"


class ModelSpec(Strict):
    """One role's model and the limits that model imposes on a call.

    Limits are per model, not per installation: once roles can differ, a single
    global context window would size a prompt for the wrong model. Legacy output
    caps are accepted for compatibility but no longer restrict generation.
    """

    model: str = Field(pattern=MODEL_ID)
    context_tokens: int | None = Field(default=None, ge=1000)
    max_output_tokens: int | None = Field(default=None, ge=256)
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class ModelProfile(Strict):
    """Operator model selection: a fallback for every role, plus overrides."""

    default: ModelSpec | None = None
    roles: dict[str, ModelSpec] = {}

    @model_validator(mode="after")
    def known_roles(self):
        unknown = set(self.roles) - set(ROLES)
        if unknown:
            raise ValueError(f"Unknown model roles: {', '.join(sorted(unknown))}")
        return self


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
    # Deprecated compatibility field: change size never restricts coverage.
    max_changed_lines: int | None = None
    token_ceiling: int = Field(default=120000, ge=1)
    timeout_s: int = Field(default=1200, ge=1, le=1200)
    unit_tokens: int = Field(default=6000, ge=256, le=20000)
    context_lines: int = 20
    max_inline: int = Field(default=15, ge=0, le=15)
    max_per_file: int = Field(default=5, ge=0, le=5)
    merge_distance: int = Field(default=10, ge=0, le=100)
    full_rereview_merge_base_delta: int = 50
    # Recheck answers the comments already on the merge request after a push.
    # The judgement cap bounds its token cost; threads past it stay open and
    # report that they could not be verified rather than guessing.
    recheck: bool = True
    recheck_max_judgements: int = Field(default=15, ge=0, le=50)
    exclude: list[str] = [
        "**/node_modules/**",
        "**/vendor/**",
        "*.min.js",
        "*.lock",
        "package-lock.json",
        "**/package-lock.json",
        "pnpm-lock.yaml",
        "**/pnpm-lock.yaml",
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
    # Operator-only controls; repository YAML cannot set these.
    analysis_mode: Literal["standard", "deep"] = "standard"
    unit_concurrency: int = Field(default=2, ge=1, le=16)
    verification_concurrency: int = Field(default=2, ge=1, le=16)
    finalization_reserve_s: float = Field(default=180, ge=0, le=600)
    publication_reserve_s: float = Field(default=30, ge=0, le=120)
    unit_timeout_s: float = Field(default=120, gt=0, le=600)
    # Legacy settings remain readable in operator files and persisted runs.
    # Scheduling covers every chunk and no output cap is sent to the gateway.
    stage_output_tokens: int | None = Field(default=None, ge=256)
    triage_max_units: int = Field(default=24, ge=1, le=200)
    triage_unit_tokens: int = Field(default=12000, ge=256, le=20000)
    final_stage_token_reserve: int = Field(default=0, ge=0)
    enforcement: Literal["silent", "advisory", "gating"] = "advisory"
    milestone: Literal["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"] = (
        "M9"
    )
    issue_tracker: IssueTrackerConfig = Field(default_factory=IssueTrackerConfig)
    documents: DocumentsConfig = Field(default_factory=DocumentsConfig)
    models: ModelProfile = Field(default_factory=ModelProfile)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    static_tools: list[StaticTool] = []
    languages: list[Literal["python", "typescript", "go"]] = [
        "python",
        "typescript",
        "go",
    ]

    @model_validator(mode="after")
    def reserve_fits_budget(self):
        if self.final_stage_token_reserve >= self.review.token_ceiling:
            raise ValueError(
                "Final stage reserve must be smaller than the review token ceiling"
            )
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://reviewer:local@postgres/reviewer"
    )
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    gitlab_base_url: str = "https://gitlab.example.invalid"
    gitlab_token: SecretStr = SecretStr("")
    git_read_token: SecretStr = SecretStr("")
    project_ids: list[PositiveInt] = Field(default_factory=list)
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
    gateway_concurrency: int = Field(default=8, ge=1, le=64)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Legacy tier configuration, still honoured for the "strong"/"fast"/
    # "verification" tiers a caller may still ask for by name.
    model_strong: str = ""
    model_fast: str = ""
    model_verifier: str = ""
    # Installation-wide role selection, above the shipped defaults and below
    # project configuration. JSON object of role -> model ID.
    model_roles: dict[str, str] = Field(default_factory=dict)
    # Per-model context and output limits by exact gateway model ID:
    # {"vendor/model": {"context_tokens": 1000000, "max_output_tokens": 65536}}.
    model_limits: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Model IDs an operator may select for a manual run. Empty means the models
    # this installation is already configured to use.
    model_catalog: list[str] = Field(default_factory=list)
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
