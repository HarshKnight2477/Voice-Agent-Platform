"""Invitations to join an organization.

Revision ID: 0003_invitations
Revises: 0002_auth_sessions
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0003_invitations"
down_revision: str | None = "0002_auth_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "voice_app"


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column(
            "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
        ),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("email", pg.CITEXT(), nullable=False),
        sa.Column("role_key", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invitations"),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_invitations_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["role_key"], ["roles.key"], name="fk_invitations_role_key_roles"),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name="fk_invitations_invited_by_user_id_users",
            ondelete="SET NULL",
        ),
    )
    # At most one open invitation per email per organization.
    op.create_index(
        "uq_invitations_open_per_email",
        "invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    )

    op.execute("ALTER TABLE invitations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE invitations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY inv_all ON invitations FOR ALL "
        "USING (organization_id = app_current_org()) "
        "WITH CHECK (organization_id = app_current_org())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON invitations TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("invitations")
