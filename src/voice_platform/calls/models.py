from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from voice_platform.infrastructure.db_base import Base, uuid_pk


def _now() -> datetime:
    return datetime.now(UTC)


def _org_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )


def _call_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )


class Call(Base):
    """One conversation with the agent: a phone call or a browser test call."""

    __tablename__ = "calls"
    __table_args__ = (
        CheckConstraint("direction IN ('inbound', 'outbound', 'test')", name="direction_values"),
        CheckConstraint("channel IN ('phone', 'web')", name="channel_values"),
        CheckConstraint(
            "status IN ('created', 'in_progress', 'completed', 'transferred', 'failed')",
            name="status_values",
        ),
        Index("ix_calls_org_created", "organization_id", "created_at"),
        Index("ix_calls_org_agent_created", "organization_id", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_versions.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="created")
    from_number: Mapped[str | None] = mapped_column(String(32))
    to_number: Mapped[str | None] = mapped_column(String(32))
    room_name: Mapped[str] = mapped_column(String(128), nullable=False)
    intent: Mapped[str | None] = mapped_column(String(60))
    outcome: Mapped[str | None] = mapped_column(String(32))
    end_reason: Mapped[str | None] = mapped_column(String(64))
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CallMessage(Base):
    """The transcript: one row per spoken turn."""

    __tablename__ = "call_messages"
    __table_args__ = (UniqueConstraint("call_id", "seq"),)
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    call_id: Mapped[uuid.UUID] = _call_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # caller | agent
    text: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class CallEvent(Base):
    """Timeline entries for the call inspector (LLM requests, tools, transfers, limits)."""

    __tablename__ = "call_events"
    __table_args__ = (UniqueConstraint("call_id", "seq"),)
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    call_id: Mapped[uuid.UUID] = _call_fk()
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )


class ToolExecution(Base):
    """Audit record for every tool request (allowed or refused). Arguments are redacted."""

    __tablename__ = "tool_executions"
    __table_args__ = (Index("ix_tool_executions_call", "call_id"),)
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    call_id: Mapped[uuid.UUID] = _call_fk()
    agent_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CallerMessage(Base):
    """A message the agent took for the business (from the take_message tool)."""

    __tablename__ = "caller_messages"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key"),
        Index("ix_caller_messages_org_created", "organization_id", "created_at"),
    )
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = _org_fk()
    call_id: Mapped[uuid.UUID] = _call_fk()
    caller_name: Mapped[str] = mapped_column(String(100), nullable=False)
    callback_number: Mapped[str] = mapped_column(String(25), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, server_default=func.now()
    )
