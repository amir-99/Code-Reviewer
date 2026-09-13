"""Reviews of Confluence pages alongside reviews of merge requests."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(JSONB, "postgresql")

TERMINAL = "('PUBLISHED','TERMINATED_EARLY','FAILED_CONTEXT','FAILED_INTERNAL','CANCELLED','SUPERSEDED')"


def upgrade():
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(10), nullable=False, server_default="code")
        )
        batch.add_column(sa.Column("subject", json_type))
        batch.add_column(sa.Column("subject_key", sa.String(200)))
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("mr_iid", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("findings") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("mr_iid", existing_type=sa.Integer(), nullable=True)
    condition = sa.text(f"kind = 'document' AND state NOT IN {TERMINAL}")
    op.create_index(
        "uq_active_document_review",
        "reviews",
        ["subject_key"],
        unique=True,
        postgresql_where=condition,
        sqlite_where=condition,
    )


def downgrade():
    op.drop_index("uq_active_document_review", table_name="reviews")
    # Document reviews cannot be represented without these columns.
    for table in (
        "review_chat_messages",
        "review_comments",
        "review_units",
        "review_events",
        "review_snapshots",
        "findings",
        "review_stages",
    ):
        op.execute(
            f"DELETE FROM {table} WHERE review_id IN "
            "(SELECT id FROM reviews WHERE kind = 'document')"
        )
    op.execute("DELETE FROM reviews WHERE kind = 'document'")
    with op.batch_alter_table("findings") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("mr_iid", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("reviews") as batch:
        batch.alter_column("project_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("mr_iid", existing_type=sa.Integer(), nullable=False)
        batch.drop_column("subject_key")
        batch.drop_column("subject")
        batch.drop_column("kind")
