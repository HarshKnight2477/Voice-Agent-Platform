from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from voice_platform.infrastructure.db_base import Base, uuid_pk


class AuditLog(Base):
    """Who did what, to what, when. Rows can be inserted but never updated or deleted
    (enforced by a trigger and by the app role's privileges)."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_org_created", "organization_id", "created_at"),)
    # Platform-level rows (null organization) are insert-only for the app role and not
    # readable by it, so inserts must not use RETURNING. Every column therefore has a
    # client-side default and nothing is fetched back after INSERT.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012

    id: Mapped[uuid.UUID] = uuid_pk()
    # Null only for platform-level events (for example a sign-up before any organization exists).
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    ip_address: Mapped[str | None] = mapped_column(INET)
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
