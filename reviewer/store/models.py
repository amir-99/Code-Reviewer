from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, text
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
