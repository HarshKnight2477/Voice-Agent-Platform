"""Auth: browser sessions and single-use email tokens.

Revision ID: 0002_auth_sessions
Revises: 0001_foundation
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002_auth_sessions"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "voice_app"


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("active_organization_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("ip_address", pg.INET(), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_user_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["active_organization_id"],
            ["organizations.id"],
            name="fk_user_sessions_active_organization_id_organizations",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])

    op.create_table(
        "user_tokens",
        sa.Column(
            "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
        ),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_user_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_tokens_user_id_users", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password')", name="ck_user_tokens_purpose_values"
        ),
    )
    op.create_index("ix_user_tokens_user_id", "user_tokens", ["user_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions, user_tokens TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("user_tokens")
    op.drop_table("user_sessions")
