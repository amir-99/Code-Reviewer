"""Review findings, publication receipts, snapshots, feedback and model audits."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    j = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), nullable=False
        ),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(32), nullable=False),
        sa.Column("data", j, nullable=False),
        sa.Column("severity_final", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("file", sa.String(), nullable=False),
    )
    op.create_index("ix_findings_review_id", "findings", ["review_id"])
    op.create_index(
        "ix_finding_identity", "findings", ["project_id", "mr_iid", "fingerprint"]
    )
    op.create_table(
        "published_comments",
        sa.Column("finding_id", sa.String(36), primary_key=True),
        sa.Column("discussion_id", sa.String(), nullable=False),
        sa.Column("note_id", sa.String(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "finding_outcomes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("mr_iid", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("labelled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("labelled_by", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_finding_outcomes_fingerprint", "finding_outcomes", ["fingerprint"]
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("review_id", sa.String(36), nullable=False),
        *[
            sa.Column(n, sa.String(), nullable=False)
            for n in (
                "stage",
                "model",
                "prompt_version",
                "prompt_hash",
                "prompt_blob_ref",
                "response_blob_ref",
                "outcome",
            )
        ],
        *[
            sa.Column(n, sa.Integer(), nullable=False)
            for n in ("tokens_in", "tokens_out", "latency_ms")
        ],
        sa.Column("cost", sa.Float(), nullable=True),
    )
    op.create_index("ix_llm_calls_review_id", "llm_calls", ["review_id"])
    op.create_table(
        "review_snapshots",
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), primary_key=True
        ),
        sa.Column("data", j, nullable=False),
    )


def downgrade():
    for name in (
        "review_snapshots",
        "llm_calls",
        "finding_outcomes",
        "published_comments",
        "findings",
    ):
        op.drop_table(name)
