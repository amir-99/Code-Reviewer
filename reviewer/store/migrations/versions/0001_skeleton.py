"""M0 durable admission, stage state, and status delivery.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gitlab_project_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("config_json", j, nullable=False),
        sa.Column("enforcement", sa.String(), nullable=False),
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "project_id", sa.Integer(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("merge_base_sha", sa.String(64)),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("decision", sa.String()),
        sa.Column("partial", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("bundle_hash", sa.String()),
        sa.Column("superseded_by", sa.String(36)),
        sa.Column("error", sa.Text()),
        sa.Column("status_delivered", sa.Boolean(), nullable=False),
        sa.Column("history", j, nullable=False),
    )
    condition = sa.text(
        "state NOT IN ('PUBLISHED','TERMINATED_EARLY','FAILED_CONTEXT','FAILED_INTERNAL','CANCELLED','SUPERSEDED')"
    )
    op.create_index(
        "uq_active_review",
        "reviews",
        ["project_id", "mr_iid"],
        unique=True,
        postgresql_where=condition,
        sqlite_where=condition,
    )
    op.create_table(
        "review_stages",
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), primary_key=True
        ),
        sa.Column("stage", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("coverage_json", j, nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String()),
    )


def downgrade():
    op.drop_table("review_stages")
    op.drop_index("uq_active_review", table_name="reviews")
    op.drop_table("reviews")
    op.drop_table("projects")
