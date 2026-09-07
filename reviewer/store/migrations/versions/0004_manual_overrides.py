"""Manually triggered reviews carry operator-supplied requirement context."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reviews",
        sa.Column(
            "overrides",
            sa.JSON().with_variant(JSONB, "postgresql"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("reviews", "overrides")
