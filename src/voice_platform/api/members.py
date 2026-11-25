"""Team management: members, role changes, removal, invitations, and the audit log list.

All routes act on the session's active organization only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from voice_platform.api.dependencies import (
    AppSettings,
    AuthContext,
    CurrentSession,
    Meta,
    SessionMaker,
    get_email_sender,
    require,
)
from voice_platform.api.errors import AppError
from voice_platform.api.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    Limit,
    Page,
    apply_keyset,
    encode_cursor,
)
from voice_platform.audit.models import AuditLog
from voice_platform.identity.email import AccountEmail, EmailSender
from voice_platform.identity.models import User, UserSession
from voice_platform.identity.service import InvalidEmailError, RequestMeta, normalize_email
from voice_platform.identity.tokens import hash_token, new_token
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.tenancy.models import Invitation, Membership
from voice_platform.tenancy.permissions import Permission, Role
from voice_platform.tenancy.rules import (
    MembershipRuleError,
    check_can_grant,
    check_removal,
    check_role_change,
)

router = APIRouter(prefix="/api/v1", tags=["members"])

INVITATION_TTL = timedelta(days=7)
Mailer = Annotated[EmailSender, Depends(get_email_sender)]


def _now() -> datetime:
    return datetime.now(UTC)


def _rule_error(exc: MembershipRuleError) -> AppError:
    return AppError(403, exc.code, exc.message)


def _audit(s: AsyncSession, ctx: AuthContext, meta: RequestMeta, action: str, **kw: object) -> None:
    s.add(
        AuditLog(
            organization_id=ctx.organization_id,
            actor_user_id=ctx.user_id,
            action=action,
            target_type=str(kw.get("target_type")) if kw.get("target_type") else None,
            target_id=str(kw["target_id"]) if kw.get("target_id") else None,
            ip_address=meta.ip,
            request_id=meta.request_id,
            details=dict(kw.get("details") or {}),  # type: ignore[call-overload]
        )
    )


# ---------------------------------------------------------------- schemas


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: Role
    joined_at: datetime


class RoleChange(BaseModel):
    role: Role


class InvitationCreate(BaseModel):
    email: str = Field(max_length=254)
    role: Role


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    created_at: datetime


class InvitationAccept(BaseModel):
    token: str = Field(min_length=40, max_length=200)


class InvitationAccepted(BaseModel):
    organization_id: uuid.UUID
    role: Role


class AuditLogOut(BaseModel):
    id: uuid.UUID
    action: str
    actor_user_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    details: dict[str, object]
    created_at: datetime


# ---------------------------------------------------------------- members


async def _role_of(s: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> Role | None:
    key = await s.scalar(
        select(Membership.role_key).where(
            Membership.organization_id == org_id, Membership.user_id == user_id
        )
    )
    return Role(key) if key else None


async def _owner_count(s: AsyncSession, org_id: uuid.UUID) -> int:
    # Lock the owners' rows so two concurrent demotions can't both pass the last-owner check.
    rows = await s.scalars(
        select(Membership.id)
        .where(Membership.organization_id == org_id, Membership.role_key == Role.OWNER.value)
        .with_for_update()
    )
    return len(rows.all())


@router.get("/organizations/current/members")
async def list_members(
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.MEMBERS_READ),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[MemberOut]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        stmt = (
            select(
                User.id,
                User.email,
                User.full_name,
                Membership.role_key,
                Membership.created_at,
                Membership.id,
            )
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.organization_id == ctx.organization_id)
        )
        rows = (
            await s.execute(apply_keyset(stmt, Membership.created_at, Membership.id, cursor, limit))
        ).all()
    page = rows[:limit]
    return Page(
        data=[
            MemberOut(user_id=u, email=e, full_name=n, role=Role(r), joined_at=j)
            for u, e, n, r, j, _ in page
        ],
        next_cursor=encode_cursor(page[-1][4], page[-1][5]) if len(rows) > limit else None,
    )


@router.patch("/organizations/current/members/{user_id}")
async def change_member_role(
    user_id: uuid.UUID,
    body: RoleChange,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.MEMBERS_UPDATE_ROLE),
) -> MemberOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        member = await s.scalar(
            select(Membership)
            .where(Membership.organization_id == ctx.organization_id, Membership.user_id == user_id)
            .with_for_update()
        )
        if member is None:
            raise AppError(404, "not_found", "Member not found.")
        current = Role(member.role_key)
        try:
            check_role_change(
                actor=ctx.role,
                target=current,
                new_role=body.role,
                is_self=user_id == ctx.user_id,
                owner_count=await _owner_count(s, ctx.organization_id),
            )
        except MembershipRuleError as exc:
            raise _rule_error(exc) from None
        member.role_key = body.role.value
        _audit(
            s,
            ctx,
            meta,
            "member.role_changed",
            target_type="user",
            target_id=user_id,
            details={"from": current.value, "to": body.role.value},
        )
        user = await s.get(User, user_id)
        await s.flush()
        assert user is not None
        return MemberOut(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=body.role,
            joined_at=member.created_at,
        )


@router.delete("/organizations/current/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    user_id: uuid.UUID,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.ORG_READ),  # leaving needs only membership
) -> None:
    is_self = user_id == ctx.user_id
    if not is_self and Permission.MEMBERS_REMOVE not in ctx.permissions:
        raise AppError(403, "forbidden", "You don't have permission to do that.")
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        member = await s.scalar(
            select(Membership)
            .where(Membership.organization_id == ctx.organization_id, Membership.user_id == user_id)
            .with_for_update()
        )
        if member is None:
            raise AppError(404, "not_found", "Member not found.")
        try:
            check_removal(
                actor=ctx.role,
                target=Role(member.role_key),
                is_self=is_self,
                owner_count=await _owner_count(s, ctx.organization_id),
            )
        except MembershipRuleError as exc:
            raise _rule_error(exc) from None
        await s.delete(member)
        # Their sessions stop pointing at this organization right away.
        await s.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.active_organization_id == ctx.organization_id,
            )
            .values(active_organization_id=None)
        )
        _audit(
            s,
            ctx,
            meta,
            "member.left" if is_self else "member.removed",
            target_type="user",
            target_id=user_id,
        )


# ---------------------------------------------------------------- invitations


@router.post("/organizations/current/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: InvitationCreate,
    sm: SessionMaker,
    meta: Meta,
    settings: AppSettings,
    mailer: Mailer,
    ctx: AuthContext = require(Permission.MEMBERS_INVITE),
) -> InvitationOut:
    try:
        email = normalize_email(body.email)
        check_can_grant(ctx.role, body.role)
    except InvalidEmailError:
        raise AppError(422, "invalid_email", "That email address isn't valid.") from None
    except MembershipRuleError as exc:
        raise _rule_error(exc) from None

    secret = new_token()
    # The organization id travels in the token so acceptance can open the right tenant
    # context; the secret part is what proves the invitation.
    token = f"{ctx.organization_id}.{secret}"
    try:
        async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
            already = await s.scalar(
                select(Membership.id)
                .join(User, User.id == Membership.user_id)
                .where(Membership.organization_id == ctx.organization_id, User.email == email)
            )
            if already is not None:
                raise AppError(409, "already_member", "That person is already a member.")
            inv = Invitation(
                organization_id=ctx.organization_id,
                email=email,
                role_key=body.role.value,
                token_hash=hash_token(token),
                invited_by_user_id=ctx.user_id,
                expires_at=_now() + INVITATION_TTL,
            )
            s.add(inv)
            await s.flush()
            await s.refresh(inv)
            _audit(
                s,
                ctx,
                meta,
                "invitation.created",
                target_type="invitation",
                target_id=inv.id,
                details={"role": body.role.value},
            )
    except IntegrityError:
        raise AppError(
            409, "invitation_exists", "There's already an open invitation for that email."
        ) from None

    await mailer.send(
        AccountEmail(
            to=email,
            kind="invitation",
            link=f"{settings.public_app_url}/accept-invitation?token={token}",
        )
    )
    return InvitationOut(
        id=inv.id,
        email=inv.email,
        role=body.role,
        expires_at=inv.expires_at,
        created_at=inv.created_at,
    )


@router.get("/organizations/current/invitations")
async def list_invitations(
    sm: SessionMaker, ctx: AuthContext = require(Permission.MEMBERS_READ)
) -> list[InvitationOut]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        rows = await s.scalars(
            select(Invitation)
            .where(
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > _now(),
            )
            .order_by(Invitation.created_at.desc())
            .limit(200)
        )
        return [
            InvitationOut(
                id=i.id,
                email=i.email,
                role=Role(i.role_key),
                expires_at=i.expires_at,
                created_at=i.created_at,
            )
            for i in rows
        ]


@router.delete(
    "/organizations/current/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.MEMBERS_INVITE),
) -> None:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        result = await s.execute(
            update(Invitation)
            .where(
                Invitation.id == invitation_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        if not result.rowcount:  # type: ignore[attr-defined]
            raise AppError(404, "not_found", "Invitation not found.")
        _audit(
            s, ctx, meta, "invitation.revoked", target_type="invitation", target_id=invitation_id
        )


@router.post("/invitations/accept")
async def accept_invitation(
    body: InvitationAccept, session: CurrentSession, sm: SessionMaker, meta: Meta
) -> InvitationAccepted:
    """The signed-in user accepts an invitation sent to their own email address."""
    invalid = AppError(400, "invalid_invitation", "This invitation is invalid or has expired.")
    org_part, _, _secret = body.token.partition(".")
    try:
        org_id = uuid.UUID(org_part)
    except ValueError:
        raise invalid from None

    async with tenant_transaction(sm, org_id=org_id, user_id=session.user_id) as s:
        inv = await s.scalar(
            select(Invitation)
            .where(
                Invitation.token_hash == hash_token(body.token),
                Invitation.organization_id == org_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > _now(),
            )
            .with_for_update()
        )
        if inv is None:
            raise invalid
        user = await s.get(User, session.user_id)
        if user is None or user.email.lower() != inv.email.lower():
            raise AppError(
                403, "wrong_account", "This invitation was sent to a different email address."
            )
        if await _role_of(s, org_id, user.id) is None:
            s.add(Membership(organization_id=org_id, user_id=user.id, role_key=inv.role_key))
        inv.accepted_at = _now()
        if user.email_verified_at is None:  # the emailed link proves the address
            user.email_verified_at = _now()
        s.add(
            AuditLog(
                organization_id=org_id,
                actor_user_id=user.id,
                action="invitation.accepted",
                target_type="invitation",
                target_id=str(inv.id),
                ip_address=meta.ip,
                request_id=meta.request_id,
            )
        )
        return InvitationAccepted(organization_id=org_id, role=Role(inv.role_key))


# ---------------------------------------------------------------- audit log


@router.get("/organizations/current/audit-logs")
async def list_audit_logs(
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.AUDIT_READ),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[AuditLogOut]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        stmt = select(AuditLog)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        rows = list(
            await s.scalars(apply_keyset(stmt, AuditLog.created_at, AuditLog.id, cursor, limit))
        )
    page = rows[:limit]
    return Page(
        data=[
            AuditLogOut(
                id=r.id,
                action=r.action,
                actor_user_id=r.actor_user_id,
                target_type=r.target_type,
                target_id=r.target_id,
                details=r.details,
                created_at=r.created_at,
            )
            for r in page
        ],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
    )
