"""Questions asked about a finished review, answered from its stored record."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade():
    op.create_table(
        "review_chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), nullable=False
        ),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer", sa.Text),
        sa.Column("citations", json_type, nullable=False),
        sa.Column("context_used", json_type, nullable=False),
        sa.Column("credential_refs", json_type),
        sa.Column("model", sa.String(200)),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("tokens_in", sa.Integer, nullable=False),
        sa.Column("tokens_out", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'answered', 'failed')", name="chat_status"
        ),
    )
    op.create_index(
        "ix_review_chat_messages_review_id", "review_chat_messages", ["review_id"]
    )
    op.create_index(
        "uq_chat_sequence",
        "review_chat_messages",
        ["review_id", "sequence"],
        unique=True,
    )


def downgrade():
    op.drop_index("uq_chat_sequence", table_name="review_chat_messages")
    op.drop_index(
        "ix_review_chat_messages_review_id", table_name="review_chat_messages"
    )
    op.drop_table("review_chat_messages")
