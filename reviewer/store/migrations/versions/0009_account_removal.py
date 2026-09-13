"""Track permanent account removal, distinct from a reversible disable."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("accounts", sa.Column("removed_at", sa.DateTime(timezone=True)))


def downgrade():
    op.drop_column("accounts", "removed_at")
