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
    "verification",
    "recheck",
    "chat",
    # Document reviews: the section reviewer and its independent verifier.
    "document_review",
    "document_verification",
    # Cheap decisions around the Defect Review: which related code and spec
    # sections each unit is shown, whether a finding's severity may be lowered,
    # and which build and test commands the sandboxed build agent runs.
    "context_planner",
    "severity_triage",
    "build_agent",
)

# Roles of the removed seven-stage deep mode. Operator files, MODEL_ROLES and
# frozen runs may still name them; they are accepted and ignored.
RETIRED_ROLES = frozenset(
    {
        "purpose",
        "design",
        "correctness",
        "complexity",
        "tests_",
        "line_review",
        "system_context",
    }
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
    "verification": "anthropic/claude-sonnet-5",
    "recheck": "google/gemini-3.8-flash",
    "chat": "google/gemini-3.8-flash",
    "document_review": "google/gemini-3.8-flash",
    "document_verification": "anthropic/claude-sonnet-5",
    "context_planner": "anthropic/claude-haiku-5.5",
    "severity_triage": "anthropic/claude-haiku-5.5",
    "build_agent": "anthropic/claude-haiku-5.5",
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

    @model_validator(mode="before")
    @classmethod
    def drop_retired_roles(cls, data):
        if isinstance(data, dict) and isinstance(data.get("roles"), dict):
            roles = {k: v for k, v in data["roles"].items() if k not in RETIRED_ROLES}
            data = {**data, "roles": roles}
        return data

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
    # Estimated tokens of linked pages in each Defect Review prompt. Larger
    # pages keep their openings and the sections most related to the change.
    prompt_tokens: int = Field(default=6000, ge=0)


class ReviewConfig(Strict):
    # Deprecated compatibility field: change size never restricts coverage.
    max_changed_lines: int | None = None
    token_ceiling: int = Field(default=120000, ge=1)
    timeout_s: int = Field(default=2400, ge=1, le=2400)
    # UTF-8 bytes of diff per Defect Review call; small files are packed together.
    unit_tokens: int = Field(default=24000, ge=256, le=100000)
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
    # The context planner attaches related code and specification sections to
    # each unit. `spec_paths` are fnmatch patterns over repository paths at the
    # reviewed head (`*` crosses directories); matching Markdown is split by
    # heading into the planner's menu.
    context_planner: bool = True
    spec_paths: list[str] = ["*.md"]
    spec_files: int = Field(default=120, ge=0, le=500)
    planned_context_bytes: int = Field(default=12000, ge=0, le=60000)
    # Severity triage may only lower a finding's code-derived severity.
    severity_triage: bool = True
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


class ChatConfig(Strict):
    """Questions asked about a finished review, paid for outside its budget.

    The token ceiling is cumulative over every question asked about one review;
    the timeout bounds a single answer. Both are separate from `review` so a
    long conversation can never eat a rerun's allowance, and a rerun's spend
    never silences the chat.
    """

    enabled: bool = True
    # Cumulative audited tokens over every question about one review. A single
    # prompt is reserved by its byte length before it is sent, so this must
    # comfortably exceed the size of one packed record.
    token_ceiling: int = Field(default=400000, ge=1000)
    timeout_s: int = Field(default=120, ge=10, le=600)
    context_rounds: int = Field(default=1, ge=0, le=2)
    messages_per_hour: int = Field(default=30, ge=1, le=500)
    history_turns: int = Field(default=6, ge=0, le=20)


class DocumentReviewConfig(Strict):
    """Reviews of Confluence pages. Operator-only; no repository YAML applies.

    The corpus is bounded twice: `documents.max_tokens` caps what is fetched, and
    `token_ceiling` caps what the run may spend reasoning over it. Space
    comparison never reads a whole space: an inventory of titles plus the top
    related pages by Confluence's own search.
    """

    token_ceiling: int = Field(default=150000, ge=1000)
    timeout_s: int = Field(default=1200, ge=60, le=2400)
    unit_tokens: int = Field(default=8000, ge=256, le=20000)
    unit_concurrency: int = Field(default=2, ge=1, le=8)
    context_rounds: int = Field(default=1, ge=0, le=3)
    # Per-finding comments posted to the page; the summary is separate.
    max_comments: int = Field(default=15, ge=0, le=30)
    # Try Confluence's undocumented inline-comment payload before a footer comment.
    inline_comments: bool = False
    space_pages: int = Field(default=30, ge=0, le=200)
    space_related: int = Field(default=5, ge=0, le=20)


class StaticTool(Strict):
    name: str
    image: str
    command: list[str]
    required: bool = False
    categories: list[str] = []
    timeout_s: int = Field(default=240, ge=1, le=1200)

    @model_validator(mode="after")
    def pinned(self):
        if "@sha256:" not in self.image or not self.command:
            raise ValueError("Static tools require a digest-pinned image and command")
        return self


Ecosystem = Literal["go", "node", "python"]


class BuildConfig(Strict):
    """Build and test the reviewed head in the sandbox. Operator-only.

    Off unless enabled here and STATIC_ENABLED points at the sandbox daemon.
    Images are chosen by the ecosystem code detects, never by the agent. The
    fetch phase is the only step with a network: a fixed, script-free download
    command per ecosystem on `fetch_network`, which the operator limits to the
    internal dependency proxy. Every agent command runs with no network.
    """

    enabled: bool = False
    images: dict[Ecosystem, str] = {}
    fetch_network: str = ""
    fetch_env: dict[str, str] = {}
    programs: list[str] = [
        "go",
        "make",
        "npm",
        "npx",
        "pnpm",
        "node",
        "python",
        "python3",
        "pytest",
        "uv",
    ]
    max_steps: int = Field(default=12, ge=1, le=40)
    max_runs: int = Field(default=6, ge=1, le=20)
    command_timeout_s: int = Field(default=300, ge=10, le=1800)
    timeout_s: int = Field(default=900, ge=30, le=2400)
    memory: str = Field(default="2g", pattern=r"^[0-9]+[kmg]$")
    cpus: str = Field(default="2", pattern=r"^[0-9]+(\.[0-9]+)?$")
    # A failed build or test check fails the deterministic decision only when
    # required, and only on a complete review in gating mode.
    required: bool = False

    @model_validator(mode="after")
    def pinned(self):
        if any("@sha256:" not in image for image in self.images.values()):
            raise ValueError("Build images must be digest-pinned")
        if any(not p or "/" in p for p in self.programs):
            raise ValueError("Build programs are bare executable names")
        return self


class ProjectConfig(Strict):
    @model_validator(mode="before")
    @classmethod
    def drop_analysis_mode(cls, data):
        # The deep/standard selector is gone: every review runs the combined
        # Defect Review. Old operator files and frozen runs still name it.
        if isinstance(data, dict) and "analysis_mode" in data:
            data = {k: v for k, v in data.items() if k != "analysis_mode"}
        return data

    # Operator-only controls; repository YAML cannot set these.
    unit_concurrency: int = Field(default=2, ge=1, le=16)
    verification_concurrency: int = Field(default=2, ge=1, le=16)
    finalization_reserve_s: float = Field(default=360, ge=0, le=1200)
    publication_reserve_s: float = Field(default=60, ge=0, le=240)
    unit_timeout_s: float = Field(default=240, gt=0, le=1200)
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
    chat: ChatConfig = Field(default_factory=ChatConfig)
    document_review: DocumentReviewConfig = Field(default_factory=DocumentReviewConfig)
    static_tools: list[StaticTool] = []
    build: BuildConfig = Field(default_factory=BuildConfig)
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
    session_origin: str = ""
    session_local_http: bool = False
    credential_keys: dict[str, SecretStr] = Field(default_factory=dict)
    credential_active_key: str = ""
    milestone: Literal["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9"] = (
        "M9"
    )
    jira_base_url: str = ""
    jira_token: SecretStr = SecretStr("")
    confluence_base_url: str = ""
    confluence_token: SecretStr = SecretStr("")
    gateway_base_url: str = ""
    gateway_auth_check_path: str = ""
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
