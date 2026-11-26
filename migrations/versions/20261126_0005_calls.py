"""Calls, transcripts, timelines, tool audit and caller messages.

Revision ID: 0005_calls
Revises: 0004_agents
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0005_calls"
down_revision: str | None = "0004_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "voice_app"
TABLES = ("calls", "call_messages", "call_events", "tool_executions", "caller_messages")


def _id() -> sa.Column[object]:
    return sa.Column(
        "id", pg.UUID(as_uuid=True), server_default=sa.func.gen_random_uuid(), nullable=False
    )


def _uuid(name: str, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(name, pg.UUID(as_uuid=True), nullable=nullable)


def _ts(name: str, nullable: bool = False, default: bool = True) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if default else None,
    )


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=f"fk_{table}_organization_id_organizations",
        ondelete="CASCADE",
    )


def _call_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["call_id"], ["calls.id"], name=f"fk_{table}_call_id_calls", ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "calls",
        _id(),
        _uuid("organization_id"),
        _uuid("agent_id"),
        _uuid("agent_version_id"),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="created", nullable=False),
        sa.Column("from_number", sa.String(32), nullable=True),
        sa.Column("to_number", sa.String(32), nullable=True),
        sa.Column("room_name", sa.String(128), nullable=False),
        sa.Column("intent", sa.String(60), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("end_reason", sa.String(64), nullable=True),
        sa.Column("turns", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        _uuid("created_by_user_id", nullable=True),
        _ts("created_at"),
        _ts("started_at", True, False),
        _ts("ended_at", True, False),
        sa.PrimaryKeyConstraint("id", name="pk_calls"),
        _org_fk("calls"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_calls_agent_id_agents", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_version_id"],
            ["agent_versions.id"],
            name="fk_calls_agent_version_id_agent_versions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_calls_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound', 'test')", name="ck_calls_direction_values"
        ),
        sa.CheckConstraint("channel IN ('phone', 'web')", name="ck_calls_channel_values"),
        sa.CheckConstraint(
            "status IN ('created', 'in_progress', 'completed', 'transferred', 'failed')",
            name="ck_calls_status_values",
        ),
    )
    op.create_index("ix_calls_org_created", "calls", ["organization_id", "created_at"])
    op.create_index(
        "ix_calls_org_agent_created", "calls", ["organization_id", "agent_id", "created_at"]
    )

    op.create_table(
        "call_messages",
        _id(),
        _uuid("organization_id"),
        _uuid("call_id"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_call_messages"),
        _org_fk("call_messages"),
        _call_fk("call_messages"),
        sa.UniqueConstraint("call_id", "seq", name="uq_call_messages_call_id_seq"),
    )
    op.create_table(
        "call_events",
        _id(),
        _uuid("organization_id"),
        _uuid("call_id"),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("data", pg.JSONB(), server_default="{}", nullable=False),
        _ts("at"),
        sa.PrimaryKeyConstraint("id", name="pk_call_events"),
        _org_fk("call_events"),
        _call_fk("call_events"),
        sa.UniqueConstraint("call_id", "seq", name="uq_call_events_call_id_seq"),
    )
    op.create_table(
        "tool_executions",
        _id(),
        _uuid("organization_id"),
        _uuid("call_id"),
        _uuid("agent_version_id"),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("arguments", pg.JSONB(), server_default="{}", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        _ts("at", default=False),
        sa.PrimaryKeyConstraint("id", name="pk_tool_executions"),
        _org_fk("tool_executions"),
        _call_fk("tool_executions"),
    )
    op.create_index("ix_tool_executions_call", "tool_executions", ["call_id"])
    op.create_table(
        "caller_messages",
        _id(),
        _uuid("organization_id"),
        _uuid("call_id"),
        sa.Column("caller_name", sa.String(100), nullable=False),
        sa.Column("callback_number", sa.String(25), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default="new", nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        _ts("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_caller_messages"),
        _org_fk("caller_messages"),
        _call_fk("caller_messages"),
        sa.UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_caller_messages_organization_id_idempotency_key",
        ),
    )
    op.create_index(
        "ix_caller_messages_org_created", "caller_messages", ["organization_id", "created_at"]
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} FOR ALL "
            "USING (organization_id = app_current_org()) "
            "WITH CHECK (organization_id = app_current_org())"
        )
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON calls, caller_messages TO {APP_ROLE}")
    # Transcript, timeline and tool audit rows are append-only for the app.
    op.execute(f"GRANT SELECT, INSERT ON call_messages, call_events, tool_executions TO {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
