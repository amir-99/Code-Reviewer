from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


json_type = JSON().with_variant(JSONB, "postgresql")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    gitlab_project_id: Mapped[int] = mapped_column(unique=True)
    config_json: Mapped[dict] = mapped_column(json_type, default=dict)
    enforcement: Mapped[str] = mapped_column(default="silent")
    onboarded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id"), index=True
    )
    trigger_source: Mapped[str] = mapped_column(
        default="system", server_default="system"
    )
    credential_refs: Mapped[dict | None] = mapped_column(json_type)
    principal_id: Mapped[str | None] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    mr_iid: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(64))
    merge_base_sha: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(default="INIT")
    decision: Mapped[str | None]
    partial: Mapped[bool] = mapped_column(default=False)
    # Operator-supplied context for a manually triggered review; NULL for hooks.
    overrides: Mapped[dict | None] = mapped_column(json_type)
    execution_config: Mapped[dict | None] = mapped_column(json_type)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bundle_hash: Mapped[str | None]
    superseded_by: Mapped[str | None] = mapped_column(String(36))
    error: Mapped[str | None] = mapped_column(Text)
    status_delivered: Mapped[bool] = mapped_column(default=False)
    history: Mapped[list] = mapped_column(json_type, default=lambda: ["INIT"])
    __table_args__ = (
        Index(
            "uq_active_review",
            "project_id",
            "mr_iid",
            unique=True,
            postgresql_where=text(
                "state NOT IN ('PUBLISHED','TERMINATED_EARLY','FAILED_CONTEXT','FAILED_INTERNAL','CANCELLED','SUPERSEDED')"
            ),
            sqlite_where=text(
                "state NOT IN ('PUBLISHED','TERMINATED_EARLY','FAILED_CONTEXT','FAILED_INTERNAL','CANCELLED','SUPERSEDED')"
            ),
        ),
    )


class ReviewStage(Base):
    __tablename__ = "review_stages"
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=0)
    coverage_json: Mapped[dict] = mapped_column(json_type, default=dict)
    tokens: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None]


class FindingRow(Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), index=True)
    project_id: Mapped[int] = mapped_column(Integer)
    mr_iid: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(json_type)
    severity_final: Mapped[str | None]
    status: Mapped[str]
    file: Mapped[str]
    __table_args__ = (
        Index("ix_finding_identity", "project_id", "mr_iid", "fingerprint"),
    )


class PublishedComment(Base):
    __tablename__ = "published_comments"
    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    discussion_id: Mapped[str]
    note_id: Mapped[str]
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FindingOutcome(Base):
    __tablename__ = "finding_outcomes"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    fingerprint: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[int]
    mr_iid: Mapped[int]
    outcome: Mapped[str]
    reason: Mapped[str]
    labelled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    labelled_by: Mapped[str]


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    review_id: Mapped[str] = mapped_column(String(36), index=True)
    stage: Mapped[str]
    model: Mapped[str]
    prompt_version: Mapped[str]
    prompt_hash: Mapped[str]
    prompt_blob_ref: Mapped[str]
    response_blob_ref: Mapped[str]
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    latency_ms: Mapped[int]
    cost: Mapped[float | None]
    outcome: Mapped[str]


class ReviewSnapshot(Base):
    __tablename__ = "review_snapshots"
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    data: Mapped[dict] = mapped_column(json_type)


class ReviewEvent(Base):
    __tablename__ = "review_events"
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(json_type)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ReviewUnit(Base):
    __tablename__ = "review_units"
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(json_type)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    login: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10))
    active: Mapped[bool] = mapped_column(default=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="account_role"),
    )


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountToken(Base):
    __tablename__ = "account_tokens"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    integration: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[str] = mapped_column(String(128))
    ciphertext: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="active")
    validation_status: Mapped[str] = mapped_column(String(20), default="unchecked")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    __table_args__ = (
        Index(
            "uq_credential_version", "user_id", "integration", "version", unique=True
        ),
    )


class ReviewTrigger(Base):
    __tablename__ = "review_triggers"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    credential_refs: Mapped[dict] = mapped_column(json_type)
    payload: Mapped[dict] = mapped_column(json_type)
    state: Mapped[str] = mapped_column(String(20), default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AccountEvent(Base):
    __tablename__ = "account_events"
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    action: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewComment(Base):
    """Durable publication state, independent of the analysis snapshot."""

    __tablename__ = "review_comments"
    review_id: Mapped[str] = mapped_column(ForeignKey("reviews.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(json_type)
