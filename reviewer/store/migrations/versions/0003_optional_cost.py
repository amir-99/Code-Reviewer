"""Unknown model pricing is NULL, never misrepresented as free usage."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("llm_calls", "cost", existing_type=sa.Float(), nullable=True)


def downgrade():
    op.execute("UPDATE llm_calls SET cost=0 WHERE cost IS NULL")
    op.alter_column("llm_calls", "cost", existing_type=sa.Float(), nullable=False)
