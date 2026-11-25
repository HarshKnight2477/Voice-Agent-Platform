from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from voice_platform.infrastructure.db_base import Base, TimestampMixin, uuid_pk


class Organization(TimestampMixin, Base):
    """A tenant: one business. Every tenant-owned row points here."""

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("data_retention_days BETWEEN 1 AND 3650", name="retention_range"),
        CheckConstraint("slug ~ '^[a-z0-9]([a-z0-9-]{1,61}[a-z0-9])?$'", name="slug_format"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="UTC")
    data_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoleRecord(Base):
    """System roles (OWNER ... VIEWER). Platform-owned catalog, read-only to the app."""

    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)


class PermissionRecord(Base):
    __tablename__ = "permissions"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.key", ondelete="CASCADE"), primary_key=True
    )
    permission_key: Mapped[str] = mapped_column(
        ForeignKey("permissions.key", ondelete="CASCADE"), primary_key=True
    )


class Membership(TimestampMixin, Base):
    """A user's role inside one organization."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        Index("ix_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_key: Mapped[str] = mapped_column(ForeignKey("roles.key"), nullable=False)


class Invitation(Base):
    """An emailed invitation to join an organization with a given role. Token stored hashed."""

    __tablename__ = "invitations"
    __table_args__ = (
        Index(
            "uq_invitations_open_per_email",
            "organization_id",
            "email",
            unique=True,
            postgresql_where="accepted_at IS NULL AND revoked_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    role_key: Mapped[str] = mapped_column(ForeignKey("roles.key"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
