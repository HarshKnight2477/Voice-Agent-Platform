"""Account flows: sign-up, login, sessions, email verification, password reset.

Every function takes a sessionmaker and opens its own transaction(s) with the narrowest
tenant context it needs. Errors are domain exceptions; the API layer maps them to HTTP.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.audit.models import AuditLog
from voice_platform.events.models import OutboxEvent
from voice_platform.identity import passwords
from voice_platform.identity.models import User, UserSession, UserToken
from voice_platform.identity.tokens import hash_token, new_token
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.tenancy.models import Membership, Organization
from voice_platform.tenancy.permissions import Role

VERIFY_EMAIL_TTL = timedelta(hours=48)
RESET_PASSWORD_TTL = timedelta(hours=1)
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)


class AuthError(Exception):
    """Base class for account-flow errors."""


class InvalidEmailError(AuthError):
    pass


class EmailTakenError(AuthError):
    pass


class InvalidCredentialsError(AuthError):
    pass


class InvalidTokenError(AuthError):
    pass


class NotAMemberError(AuthError):
    pass


@dataclass(frozen=True)
class RequestMeta:
    ip: str | None
    user_agent: str | None
    request_id: str | None


@dataclass(frozen=True)
class NewSession:
    token: str
    session_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None


@dataclass(frozen=True)
class ActiveSession:
    session_id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_email(raw: str) -> str:
    try:
        return validate_email(raw, check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise InvalidEmailError(str(exc)) from exc


def _slug_for(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "org"
    return f"{base}-{secrets.token_hex(3)}"


def _audit(
    s: AsyncSession,
    action: str,
    meta: RequestMeta,
    *,
    org_id: uuid.UUID | None = None,
    actor: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object = None,
) -> None:
    s.add(
        AuditLog(
            organization_id=org_id,
            actor_user_id=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            ip_address=meta.ip,
            request_id=meta.request_id,
        )
    )


def _new_session_row(
    s: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID | None,
    meta: RequestMeta,
    ttl: timedelta,
) -> NewSession:
    token = new_token()
    row = UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=hash_token(token),
        active_organization_id=org_id,
        ip_address=meta.ip,
        user_agent=(meta.user_agent or "")[:300] or None,
        expires_at=_now() + ttl,
    )
    s.add(row)
    return NewSession(token, row.id, user_id, org_id)


def _new_email_token(s: AsyncSession, user_id: uuid.UUID, purpose: str, ttl: timedelta) -> str:
    token = new_token()
    s.add(
        UserToken(
            user_id=user_id, purpose=purpose, token_hash=hash_token(token), expires_at=_now() + ttl
        )
    )
    return token


# ---------------------------------------------------------------- sign-up / login


@dataclass(frozen=True)
class SignupResult:
    session: NewSession
    verify_token: str
    email: str


async def signup(
    sm: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    full_name: str,
    organization_name: str,
    meta: RequestMeta,
    session_ttl: timedelta,
) -> SignupResult:
    """Create the user, their first organization (as OWNER) and a signed-in session."""
    email = normalize_email(email)
    passwords.check_policy(password, email=email)
    user_id, org_id = uuid.uuid4(), uuid.uuid4()

    async with tenant_transaction(sm, org_id=org_id, user_id=user_id) as s:
        if await s.scalar(select(User.id).where(User.email == email)) is not None:
            raise EmailTakenError(email)
        s.add(
            User(
                id=user_id,
                email=email,
                full_name=full_name.strip(),
                password_hash=passwords.hash_password(password),
            )
        )
        await s.flush()
        s.add(
            Organization(
                id=org_id, name=organization_name.strip(), slug=_slug_for(organization_name)
            )
        )
        await s.flush()
        s.add(Membership(organization_id=org_id, user_id=user_id, role_key=Role.OWNER.value))
        _audit(
            s,
            "user.signed_up",
            meta,
            org_id=org_id,
            actor=user_id,
            target_type="user",
            target_id=user_id,
        )
        _audit(
            s,
            "organization.created",
            meta,
            org_id=org_id,
            actor=user_id,
            target_type="organization",
            target_id=org_id,
        )
        s.add(
            OutboxEvent(
                organization_id=org_id,
                event_type="organization.created",
                payload={"organization_id": str(org_id), "owner_user_id": str(user_id)},
                trace_id=meta.request_id,
            )
        )
        session = _new_session_row(s, user_id, org_id, meta, session_ttl)
        verify_token = _new_email_token(s, user_id, "verify_email", VERIFY_EMAIL_TTL)
    return SignupResult(session, verify_token, email)


async def login(
    sm: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
    meta: RequestMeta,
    session_ttl: timedelta,
) -> NewSession:
    try:
        email = normalize_email(email)
    except InvalidEmailError:
        passwords.verify_password(password, None)  # same work as a real check
        raise InvalidCredentialsError from None

    async with tenant_transaction(sm, org_id=None, user_id=None) as s:
        user = await s.scalar(select(User).where(User.email == email))
        ok = passwords.verify_password(password, user.password_hash if user else None)
        if user is None or not ok or not user.is_active or user.deleted_at is not None:
            _audit(s, "user.login_failed", meta, actor=user.id if user else None)
            raise_after = True
        else:
            raise_after = False
            if user.password_hash and passwords.needs_rehash(user.password_hash):
                user.password_hash = passwords.hash_password(password)
    if raise_after:
        raise InvalidCredentialsError

    assert user is not None
    async with tenant_transaction(sm, org_id=None, user_id=user.id) as s:
        first_org = await s.scalar(
            select(Membership.organization_id)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
            .limit(1)
        )
        session = _new_session_row(s, user.id, first_org, meta, session_ttl)
        _audit(s, "user.logged_in", meta, org_id=None, actor=user.id)
    return session


# ---------------------------------------------------------------- sessions


async def resolve_session(sm: async_sessionmaker[AsyncSession], token: str) -> ActiveSession | None:
    now = _now()
    async with tenant_transaction(sm, org_id=None, user_id=None) as s:
        row = await s.scalar(
            select(UserSession)
            .join(User, User.id == UserSession.user_id)
            .where(
                UserSession.token_hash == hash_token(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        if row is None:
            return None
        if now - row.last_seen_at > SESSION_TOUCH_INTERVAL:
            row.last_seen_at = now
        return ActiveSession(row.id, row.user_id, row.active_organization_id)


async def logout(
    sm: async_sessionmaker[AsyncSession], session: ActiveSession, meta: RequestMeta
) -> None:
    async with tenant_transaction(sm, org_id=session.organization_id, user_id=session.user_id) as s:
        await s.execute(
            update(UserSession)
            .where(UserSession.id == session.session_id)
            .values(revoked_at=_now())
        )
        _audit(s, "user.logged_out", meta, org_id=session.organization_id, actor=session.user_id)


async def switch_organization(
    sm: async_sessionmaker[AsyncSession],
    session: ActiveSession,
    organization_id: uuid.UUID,
    meta: RequestMeta,
    session_ttl: timedelta,
) -> NewSession:
    """Move the session to another organization the user belongs to. The session token is
    rotated, because the session's privileges change. The transaction runs in the target
    organization's context; if the user is not a member it raises and nothing is written."""
    async with tenant_transaction(sm, org_id=organization_id, user_id=session.user_id) as s:
        is_member = await s.scalar(
            select(Membership.id).where(
                Membership.user_id == session.user_id,
                Membership.organization_id == organization_id,
            )
        )
        if is_member is None:
            raise NotAMemberError
        await s.execute(
            update(UserSession)
            .where(UserSession.id == session.session_id)
            .values(revoked_at=_now())
        )
        new = _new_session_row(s, session.user_id, organization_id, meta, session_ttl)
        _audit(
            s, "session.organization_switched", meta, org_id=organization_id, actor=session.user_id
        )
    return new


async def list_sessions(
    sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> list[UserSession]:
    async with tenant_transaction(sm, org_id=None, user_id=user_id) as s:
        rows = await s.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > _now(),
            )
            .order_by(UserSession.last_seen_at.desc())
        )
        return list(rows)


async def revoke_session(
    sm: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    meta: RequestMeta,
) -> bool:
    async with tenant_transaction(sm, org_id=None, user_id=user_id) as s:
        result = await s.execute(
            update(UserSession)
            .where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,  # only your own sessions
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=_now())
        )
        if result.rowcount:  # type: ignore[attr-defined]
            _audit(
                s,
                "session.revoked",
                meta,
                actor=user_id,
                target_type="session",
                target_id=session_id,
            )
        return bool(result.rowcount)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- email tokens


async def _consume_token(s: AsyncSession, token: str, purpose: str) -> UserToken:
    row = await s.scalar(
        select(UserToken)
        .where(
            UserToken.token_hash == hash_token(token),
            UserToken.purpose == purpose,
            UserToken.used_at.is_(None),
            UserToken.expires_at > _now(),
        )
        .with_for_update()
    )
    if row is None:
        raise InvalidTokenError
    row.used_at = _now()
    return row


async def verify_email(sm: async_sessionmaker[AsyncSession], token: str, meta: RequestMeta) -> None:
    async with tenant_transaction(sm, org_id=None, user_id=None) as s:
        row = await _consume_token(s, token, "verify_email")
        user = await s.get(User, row.user_id)
        if user is not None and user.email_verified_at is None:
            user.email_verified_at = _now()
        _audit(s, "user.email_verified", meta, actor=row.user_id)


async def new_verification_token(sm: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> str:
    async with tenant_transaction(sm, org_id=None, user_id=user_id) as s:
        return _new_email_token(s, user_id, "verify_email", VERIFY_EMAIL_TTL)


async def request_password_reset(
    sm: async_sessionmaker[AsyncSession], email: str, meta: RequestMeta
) -> tuple[str, str] | None:
    """Returns (email, token) if a reset should be sent. Callers must respond identically
    either way, so the endpoint does not reveal which emails have accounts."""
    try:
        email = normalize_email(email)
    except InvalidEmailError:
        return None
    async with tenant_transaction(sm, org_id=None, user_id=None) as s:
        user = await s.scalar(
            select(User).where(User.email == email, User.is_active, User.deleted_at.is_(None))
        )
        if user is None:
            return None
        # Only the newest reset link works.
        await s.execute(
            update(UserToken)
            .where(
                UserToken.user_id == user.id,
                UserToken.purpose == "reset_password",
                UserToken.used_at.is_(None),
            )
            .values(used_at=_now())
        )
        token = _new_email_token(s, user.id, "reset_password", RESET_PASSWORD_TTL)
        _audit(s, "user.password_reset_requested", meta, actor=user.id)
    return email, token


async def reset_password(
    sm: async_sessionmaker[AsyncSession], token: str, new_password: str, meta: RequestMeta
) -> None:
    async with tenant_transaction(sm, org_id=None, user_id=None) as s:
        row = await _consume_token(s, token, "reset_password")
        user = await s.get(User, row.user_id)
        if user is None:
            raise InvalidTokenError
        passwords.check_policy(new_password, email=user.email)
        user.password_hash = passwords.hash_password(new_password)
        # Sign out everywhere: whoever knew the old password loses access.
        await s.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=_now())
        )
        _audit(s, "user.password_reset", meta, actor=user.id)
