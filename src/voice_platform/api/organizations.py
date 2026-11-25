"""/api/v1/me and /api/v1/organizations/current: the first permission-checked endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import available_timezones

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from voice_platform.api.dependencies import (
    SESSION_COOKIE,
    AppSettings,
    AuthContext,
    CurrentSession,
    SessionMaker,
    require,
)
from voice_platform.api.errors import AppError
from voice_platform.audit.models import AuditLog
from voice_platform.identity.models import User
from voice_platform.identity.tokens import csrf_token_for
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.tenancy.models import Membership, Organization
from voice_platform.tenancy.permissions import Permission, Role, permissions_for

router = APIRouter(prefix="/api/v1", tags=["organizations"])

_TIMEZONES = frozenset(available_timezones())


class MembershipSummary(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    role: Role


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    email_verified: bool
    active_organization_id: uuid.UUID | None
    role: Role | None
    permissions: list[Permission]
    memberships: list[MembershipSummary]
    csrf_token: str


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    data_retention_days: int
    created_at: datetime


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    timezone: str | None = None
    data_retention_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        if v is not None and v not in _TIMEZONES:
            raise ValueError("Unknown time zone. Use an IANA name such as Asia/Kolkata.")
        return v


@router.get("/me")
async def me(
    request: Request, session: CurrentSession, sm: SessionMaker, settings: AppSettings
) -> MeResponse:
    async with tenant_transaction(sm, org_id=None, user_id=session.user_id) as s:
        user = await s.get(User, session.user_id)
        rows = (
            await s.execute(
                select(Membership.organization_id, Organization.name, Membership.role_key)
                .join(Organization, Organization.id == Membership.organization_id)
                .where(Membership.user_id == session.user_id, Organization.deleted_at.is_(None))
                .order_by(Organization.name)
            )
        ).all()
    assert user is not None
    memberships = [
        MembershipSummary(organization_id=o, organization_name=n, role=Role(r)) for o, n, r in rows
    ]
    active = next((m for m in memberships if m.organization_id == session.organization_id), None)
    token = request.cookies[SESSION_COOKIE]
    return MeResponse(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        email_verified=user.email_verified_at is not None,
        active_organization_id=active.organization_id if active else None,
        role=active.role if active else None,
        permissions=sorted(permissions_for(active.role)) if active else [],
        memberships=memberships,
        csrf_token=csrf_token_for(token, settings.app_secret_key.get_secret_value()),
    )


def _org_out(org: Organization) -> OrganizationOut:
    return OrganizationOut(
        id=org.id,
        name=org.name,
        slug=org.slug,
        timezone=org.timezone,
        data_retention_days=org.data_retention_days,
        created_at=org.created_at,
    )


@router.get("/organizations/current")
async def get_current_organization(
    sm: SessionMaker, ctx: AuthContext = require(Permission.ORG_READ)
) -> OrganizationOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        org = await s.get(Organization, ctx.organization_id)
    if org is None or org.deleted_at is not None:
        raise AppError(404, "not_found", "Organization not found.")
    return _org_out(org)


@router.patch("/organizations/current")
async def update_current_organization(
    body: OrganizationUpdate,
    request: Request,
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.ORG_UPDATE),
) -> OrganizationOut:
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        org = await s.get(Organization, ctx.organization_id, with_for_update=True)
        if org is None or org.deleted_at is not None:
            raise AppError(404, "not_found", "Organization not found.")
        for field, value in changes.items():
            setattr(org, field, value)
        if changes:
            s.add(
                AuditLog(
                    organization_id=ctx.organization_id,
                    actor_user_id=ctx.user_id,
                    action="organization.updated",
                    target_type="organization",
                    target_id=str(ctx.organization_id),
                    request_id=request.scope.get("state", {}).get("request_id"),
                    details={"fields": sorted(changes)},
                )
            )
        await s.flush()
        await s.refresh(org)
    return _org_out(org)
