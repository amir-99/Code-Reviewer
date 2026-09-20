"""Cache project display names without changing project policy."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("name", sa.String(), nullable=True))
    op.add_column(
        "projects", sa.Column("path_with_namespace", sa.String(), nullable=True)
    )


def downgrade():
    op.drop_column("projects", "path_with_namespace")
    op.drop_column("projects", "name")
