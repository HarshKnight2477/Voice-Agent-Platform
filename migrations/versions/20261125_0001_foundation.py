"""Foundation: users, organizations, roles, permissions, memberships, audit log, outbox, RLS.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "voice_app"

# Frozen snapshot of voice_platform.tenancy.permissions at the time of this migration.
# Later catalog changes must ship as new migrations, never by editing this one.
ROLES = [
    ("OWNER", "Owner", 100),
    ("ADMIN", "Admin", 80),
    ("MANAGER", "Manager", 60),
    ("AGENT_MANAGER", "Agent manager", 40),
    ("ANALYST", "Analyst", 20),
    ("VIEWER", "Viewer", 10),
]
PERMISSIONS = {
    "org.read": "View organization details",
    "org.update": "Change organization settings",
    "org.delete": "Delete the organization",
    "billing.manage": "Manage plan, payment and invoices",
    "members.read": "View members",
    "members.invite": "Invite members",
    "members.update_role": "Change a member's role",
    "members.remove": "Remove members",
    "agents.read": "View agents",
    "agents.write": "Create and edit agents",
    "agents.publish": "Publish and roll back agent versions",
    "knowledge.read": "View knowledge bases",
    "knowledge.write": "Upload and edit knowledge",
    "phone_numbers.read": "View phone numbers",
    "phone_numbers.manage": "Add, assign and release phone numbers and carriers",
    "calls.read": "View call history and metrics",
    "calls.transcript.read": "Read call transcripts",
    "recordings.read": "Listen to call recordings",
    "recordings.download": "Download call recordings",
    "analytics.read": "View analytics",
    "integrations.manage": "Connect and configure integrations",
    "api_keys.manage": "Create and revoke API keys",
    "audit.read": "View the audit log",
}
_ALL = set(PERMISSIONS)
ROLE_PERMISSIONS = {
    "OWNER": _ALL,
    "ADMIN": _ALL - {"org.delete", "billing.manage"},
    "MANAGER": {
        "org.read", "members.read", "members.invite", "members.update_role", "members.remove",
        "agents.read", "agents.write", "agents.publish", "knowledge.read", "knowledge.write",
        "phone_numbers.read", "phone_numbers.manage", "calls.read", "calls.transcript.read",
        "recordings.read", "analytics.read", "integrations.manage",
    },
    "AGENT_MANAGER": {
        "org.read", "members.read", "agents.read", "agents.write", "agents.publish",
        "knowledge.read", "knowledge.write", "phone_numbers.read", "calls.read",
        "calls.transcript.read", "analytics.read",
    },
    "ANALYST": {
        "org.read", "agents.read", "knowledge.read", "calls.read", "calls.transcript.read",
        "analytics.read",
    },
    "VIEWER": {"org.read", "agents.read", "calls.read", "analytics.read"},
}  # fmt: skip


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]  # fmt: skip


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # The runtime role. Created without login here; `db-setup` gives it LOGIN + a password.
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

    # Helpers used by every policy. NULL when unset, so policies fail closed.
    op.execute(
        "CREATE FUNCTION app_current_org() RETURNS uuid LANGUAGE sql STABLE AS "
        "$$ SELECT NULLIF(current_setting('app.org_id', true), '')::uuid $$"
    )
    op.execute(
        "CREATE FUNCTION app_current_user() RETURNS uuid LANGUAGE sql STABLE AS "
        "$$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$"
    )

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("email", pg.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("full_name", sa.String(200), server_default="", nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "organizations",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", pg.CITEXT(), nullable=False),
        sa.Column("timezone", sa.String(64), server_default="UTC", nullable=False),
        sa.Column("data_retention_days", sa.Integer(), server_default="90", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.UniqueConstraint("slug", name="uq_organizations_slug"),
        sa.CheckConstraint(
            "data_retention_days BETWEEN 1 AND 3650", name="ck_organizations_retention_range"
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$'", name="ck_organizations_slug_format"
        ),
    )

    op.create_table(
        "roles",
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_roles"),
        sa.UniqueConstraint("rank", name="uq_roles_rank"),
    )
    op.create_table(
        "permissions",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("description", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_permissions"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_key", sa.String(32), nullable=False),
        sa.Column("permission_key", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_key"], ["roles.key"], name="fk_role_permissions_role_key_roles", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["permission_key"], ["permissions.key"],
            name="fk_role_permissions_permission_key_permissions", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_key", "permission_key", name="pk_role_permissions"),
    )  # fmt: skip

    op.create_table(
        "memberships",
        _uuid_pk(),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("role_key", sa.String(32), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_memberships"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_memberships_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_memberships_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_key"], ["roles.key"], name="fk_memberships_role_key_roles"),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_memberships_organization_id_user_id"),
    )  # fmt: skip
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    op.create_table(
        "audit_logs",
        _uuid_pk(),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_user_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(50), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("ip_address", pg.INET(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("details", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_audit_logs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"],
            name="fk_audit_logs_actor_user_id_users", ondelete="SET NULL",
        ),
    )  # fmt: skip
    op.create_index("ix_audit_logs_org_created", "audit_logs", ["organization_id", "created_at"])

    op.create_table(
        "outbox_events",
        _uuid_pk(),
        sa.Column("organization_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("event_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_outbox_events_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatched', 'failed')", name="ck_outbox_events_status_values"
        ),
    )  # fmt: skip
    op.create_index(
        "ix_outbox_events_pending",
        "outbox_events",
        ["occurred_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Seed the role and permission catalog.
    op.bulk_insert(
        sa.table("roles", sa.column("key"), sa.column("name"), sa.column("rank")),
        [{"key": k, "name": n, "rank": r} for k, n, r in ROLES],
    )
    op.bulk_insert(
        sa.table("permissions", sa.column("key"), sa.column("description")),
        [{"key": k, "description": d} for k, d in PERMISSIONS.items()],
    )
    op.bulk_insert(
        sa.table("role_permissions", sa.column("role_key"), sa.column("permission_key")),
        [
            {"role_key": role, "permission_key": perm}
            for role, perms in ROLE_PERMISSIONS.items()
            for perm in sorted(perms)
        ],
    )

    # Audit log is append-only. The purge flag exists only for the retention job (owner role).
    op.execute(
        """
        CREATE FUNCTION audit_logs_block_changes() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF current_setting('app.allow_audit_purge', true) = 'on' AND TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION 'audit_logs is append-only';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_block_changes()"
    )

    # Row-level security. FORCE makes it apply to the table owner too (superusers still bypass,
    # which is why the API never connects as one).
    for table in ("organizations", "memberships", "audit_logs", "outbox_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    policies = [  # one statement each: asyncpg runs a single command per call
        # organizations: visible if it is the active org, or the user is a member of it.
        'CREATE POLICY org_select ON organizations FOR SELECT USING (id = app_current_org() OR id IN (SELECT organization_id FROM memberships WHERE user_id = app_current_user()))',
        'CREATE POLICY org_insert ON organizations FOR INSERT WITH CHECK (id = app_current_org())',
        'CREATE POLICY org_update ON organizations FOR UPDATE USING (id = app_current_org()) WITH CHECK (id = app_current_org())',
        'CREATE POLICY org_delete ON organizations FOR DELETE USING (id = app_current_org())',
        # memberships: the active org's members, plus the user's own memberships elsewhere.
        'CREATE POLICY mem_select ON memberships FOR SELECT USING (organization_id = app_current_org() OR user_id = app_current_user())',
        'CREATE POLICY mem_insert ON memberships FOR INSERT WITH CHECK (organization_id = app_current_org())',
        'CREATE POLICY mem_update ON memberships FOR UPDATE USING (organization_id = app_current_org()) WITH CHECK (organization_id = app_current_org())',
        'CREATE POLICY mem_delete ON memberships FOR DELETE USING (organization_id = app_current_org())',
        # audit_logs: read own org only; write own org or platform-level (null org).
        'CREATE POLICY audit_select ON audit_logs FOR SELECT USING (organization_id = app_current_org())',
        'CREATE POLICY audit_insert ON audit_logs FOR INSERT WITH CHECK (organization_id = app_current_org() OR organization_id IS NULL)',
        # outbox_events: same shape as audit, plus status updates within the org.
        'CREATE POLICY outbox_select ON outbox_events FOR SELECT USING (organization_id = app_current_org())',
        'CREATE POLICY outbox_insert ON outbox_events FOR INSERT WITH CHECK (organization_id = app_current_org() OR organization_id IS NULL)',
        'CREATE POLICY outbox_update ON outbox_events FOR UPDATE USING (organization_id = app_current_org()) WITH CHECK (organization_id = app_current_org())',
    ]  # fmt: skip
    for statement in policies:
        op.execute(statement)

    # Least-privilege grants for the runtime role.
    for statement in (
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON users, organizations, memberships TO {APP_ROLE}",
        f"GRANT SELECT, INSERT ON audit_logs TO {APP_ROLE}",
        f"GRANT SELECT, INSERT, UPDATE ON outbox_events TO {APP_ROLE}",
        f"GRANT SELECT ON roles, permissions, role_permissions TO {APP_ROLE}",
        f"GRANT EXECUTE ON FUNCTION app_current_org(), app_current_user() TO {APP_ROLE}",
    ):
        op.execute(statement)


def downgrade() -> None:
    # org_select reads memberships, so drop it before the tables it depends on.
    op.execute("DROP POLICY IF EXISTS org_select ON organizations")
    for table in (
        "outbox_events",
        "audit_logs",
        "memberships",
        "role_permissions",
        "permissions",
        "roles",
        "organizations",
        "users",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS audit_logs_block_changes()")
    op.execute("DROP FUNCTION IF EXISTS app_current_org()")
    op.execute("DROP FUNCTION IF EXISTS app_current_user()")
    # The voice_app role is cluster-wide and may own nothing; it is left in place on purpose.
