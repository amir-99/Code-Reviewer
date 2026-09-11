"""Checkpoint validated work units independently of stage completion."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reviews",
        sa.Column(
            "execution_config",
            sa.JSON().with_variant(JSONB, "postgresql"),
            nullable=True,
        ),
    )
    op.create_table(
        "review_units",
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), primary_key=True
        ),
        sa.Column("stage", sa.String(32), primary_key=True),
        sa.Column("input_hash", sa.String(64), primary_key=True),
        sa.Column("data", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
    )


def downgrade():
    op.drop_table("review_units")
    op.drop_column("reviews", "execution_config")
