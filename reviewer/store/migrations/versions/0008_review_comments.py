"""Track editable finding comments and overall review messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "review_comments",
        sa.Column(
            "review_id", sa.String(36), sa.ForeignKey("reviews.id"), primary_key=True
        ),
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("data", sa.JSON().with_variant(JSONB, "postgresql"), nullable=False),
    )


def downgrade():
    op.drop_table("review_comments")
