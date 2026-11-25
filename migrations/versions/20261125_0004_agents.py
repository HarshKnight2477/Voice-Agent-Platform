"""Agents, draft configurations and immutable versions.

Revision ID: 0004_agents
Revises: 0003_invitations
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004_agents"
down_revision: str | None = "0003_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "voice_app"
TENANT_TABLES = ("agents", "agent_configurations", "agent_versions")


def _uuid(name: str, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, pg.UUID(as_uuid=True), nullable=nullable)


def _now(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column(
            "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
        ),
        _uuid("organization_id"),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("template_key", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        _uuid("published_version_id", nullable=True),
        _uuid("created_by_user_id", nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_agents_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_agents_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_agents_status_values"),
    )
    op.create_index("ix_agents_org_created", "agents", ["organization_id", "created_at"])

    op.create_table(
        "agent_configurations",
        _uuid("agent_id"),
        _uuid("organization_id"),
        sa.Column("config", pg.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        _uuid("updated_by_user_id", nullable=True),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("agent_id", name="pk_agent_configurations"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_configurations_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_agent_configurations_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_agent_configurations_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
    )

    op.create_table(
        "agent_versions",
        sa.Column(
            "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
        ),
        _uuid("organization_id"),
        _uuid("agent_id"),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="testing", nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("change_note", sa.String(500), server_default="", nullable=False),
        _uuid("created_by_user_id", nullable=True),
        _now("created_at"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agent_versions"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_agent_versions_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_versions_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_agent_versions_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("agent_id", "number", name="uq_agent_versions_agent_id_number"),
        sa.CheckConstraint(
            "status IN ('testing', 'published', 'retired')", name="ck_agent_versions_status_values"
        ),
    )
    op.create_index(
        "ix_agent_versions_org_agent", "agent_versions", ["organization_id", "agent_id"]
    )
    op.create_foreign_key(
        "fk_agents_published_version_id_agent_versions",
        "agents",
        "agent_versions",
        ["published_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Versions are immutable: only status and published_at may change, and rows are never deleted
    # directly (they go away only with their agent or organization, via cascade).
    op.execute(
        """
        CREATE FUNCTION agent_versions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'UPDATE' AND (
               NEW.snapshot IS DISTINCT FROM OLD.snapshot
            OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
            OR NEW.number IS DISTINCT FROM OLD.number
            OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
            OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
            OR NEW.change_note IS DISTINCT FROM OLD.change_note
            OR NEW.created_at IS DISTINCT FROM OLD.created_at) THEN
            RAISE EXCEPTION 'agent_versions are immutable';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_versions_immutable BEFORE UPDATE ON agent_versions "
        "FOR EACH ROW EXECUTE FUNCTION agent_versions_guard()"
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} FOR ALL "
            "USING (organization_id = app_current_org()) "
            "WITH CHECK (organization_id = app_current_org())"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON agents, agent_configurations TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON agent_versions TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_constraint(
        "fk_agents_published_version_id_agent_versions", "agents", type_="foreignkey"
    )
    op.drop_table("agent_versions")
    op.drop_table("agent_configurations")
    op.drop_table("agents")
    op.execute("DROP FUNCTION IF EXISTS agent_versions_guard()")
