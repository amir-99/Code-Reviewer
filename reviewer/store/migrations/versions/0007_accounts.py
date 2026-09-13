"""Local accounts, durable sessions and personal review ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    # Freeze the table definitions in this migration, independently of future models.
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("login", sa.String(254), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("password_hash", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'user')", name="account_role"),
    )
    for name in ("login_sessions", "account_tokens"):
        columns = [
            sa.Column("token_hash", sa.String(64), primary_key=True),
            sa.Column(
                "user_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        ]
        if name == "login_sessions":
            columns.append(sa.Column("csrf_hash", sa.String(64), nullable=False))
        op.create_table(name, *columns)
        op.create_index(f"ix_{name}_user_id", name, ["user_id"])
    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("integration", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("key_id", sa.String(128), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("validation_status", sa.String(20), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_integration_credentials_user_id", "integration_credentials", ["user_id"]
    )
    op.create_index(
        "uq_credential_version",
        "integration_credentials",
        ["user_id", "integration", "version"],
        unique=True,
    )
    json_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "review_triggers",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column(
            "owner_user_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("credential_refs", json_type, nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_review_triggers_owner_user_id", "review_triggers", ["owner_user_id"]
    )
    op.create_table(
        "account_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("accounts.id")),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column(
        "reviews",
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", name="fk_reviews_owner"),
        ),
    )
    op.add_column(
        "reviews",
        sa.Column(
            "trigger_source", sa.String(), nullable=False, server_default="system"
        ),
    )
    op.add_column("reviews", sa.Column("credential_refs", json_type))
    op.add_column("reviews", sa.Column("principal_id", sa.String(128)))
    op.create_index("ix_reviews_owner_user_id", "reviews", ["owner_user_id"])


def downgrade():
    op.drop_index("ix_reviews_owner_user_id", "reviews")
    for column in (
        "principal_id",
        "credential_refs",
        "trigger_source",
        "owner_user_id",
    ):
        op.drop_column("reviews", column)
    for table in (
        "login_throttles",
        "account_events",
        "review_triggers",
        "integration_credentials",
        "account_tokens",
        "login_sessions",
        "accounts",
    ):
        op.drop_table(table)
