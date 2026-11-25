"""Request dependencies: database, settings, current session, tenant context, permission checks.

Tenant identity comes only from the server-side session (cookie -> session row -> active
organization -> membership). Nothing in the URL or body can choose the organization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.api.errors import AppError
from voice_platform.identity import service
from voice_platform.identity.email import EmailSender
from voice_platform.identity.tokens import csrf_matches
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.shared.config import Settings, get_settings
from voice_platform.shared.logging import get_logger
from voice_platform.shared.rate_limit import RateLimiter
from voice_platform.tenancy.models import Membership
from voice_platform.tenancy.permissions import Permission, Role, permissions_for

log = get_logger(__name__)

SESSION_COOKIE = "vp_session"
CSRF_COOKIE = "vp_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return sm


def get_email_sender(request: Request) -> EmailSender:
    sender: EmailSender = request.app.state.email_sender
    return sender


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


SessionMaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def request_meta(request: Request) -> service.RequestMeta:
    return service.RequestMeta(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=request.scope.get("state", {}).get("request_id"),
    )


Meta = Annotated[service.RequestMeta, Depends(request_meta)]


def unauthenticated() -> AppError:
    return AppError(401, "unauthenticated", "Please sign in.")


async def current_session(
    request: Request, sm: SessionMaker, settings: AppSettings
) -> service.ActiveSession:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise unauthenticated()
    session = await service.resolve_session(sm, token)
    if session is None:
        raise unauthenticated()
    # CSRF: state-changing requests must echo the token derived from this session.
    if request.method not in SAFE_METHODS:
        provided = request.headers.get(CSRF_HEADER, "")
        if not provided or not csrf_matches(
            provided, token, settings.app_secret_key.get_secret_value()
        ):
            raise AppError(403, "csrf_failed", "Missing or invalid CSRF token.")
    return session


CurrentSession = Annotated[service.ActiveSession, Depends(current_session)]


@dataclass(frozen=True)
class AuthContext:
    """Who is acting, for which organization, with which permissions."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    permissions: frozenset[Permission]


async def auth_context(session: CurrentSession, sm: SessionMaker) -> AuthContext:
    if session.organization_id is None:
        raise AppError(403, "no_organization", "Select an organization first.")
    async with tenant_transaction(sm, org_id=session.organization_id, user_id=session.user_id) as s:
        role_key = await s.scalar(
            select(Membership.role_key).where(
                Membership.organization_id == session.organization_id,
                Membership.user_id == session.user_id,
            )
        )
    if role_key is None:  # membership removed since the session started
        raise AppError(403, "not_a_member", "You no longer have access to this organization.")
    role = Role(role_key)
    return AuthContext(
        user_id=session.user_id,
        session_id=session.session_id,
        organization_id=session.organization_id,
        role=role,
        permissions=permissions_for(role),
    )


Auth = Annotated[AuthContext, Depends(auth_context)]


def require(permission: Permission) -> Any:  # returns Depends(...)
    """Dependency factory: `Depends(require(Permission.AGENTS_WRITE))`."""

    async def _check(ctx: Auth) -> AuthContext:
        if permission not in ctx.permissions:
            log.warning(
                "authz.denied",
                user_id=str(ctx.user_id),
                organization_id=str(ctx.organization_id),
                role=ctx.role.value,
                permission=permission.value,
            )
            raise AppError(403, "forbidden", "You don't have permission to do that.")
        return ctx

    return Depends(_check)


async def require_json_body(request: Request) -> None:
    """Unauthenticated state-changing endpoints accept JSON only. Browsers cannot send a
    cross-site JSON POST without a CORS preflight, which closes login/sign-up CSRF."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise AppError(415, "unsupported_media_type", "Send the request body as JSON.")


async def rate_limit(limiter: RateLimiter, key: str, *, limit: int, window_seconds: int) -> None:
    try:
        allowed = await limiter.hit(key, limit=limit, window_seconds=window_seconds)
    except Exception as exc:  # Redis down: fail open, but loudly.
        log.error("rate_limit.unavailable", error=type(exc).__name__)
        return
    if not allowed:
        raise AppError(
            429,
            "rate_limited",
            "Too many attempts. Please wait and try again.",
            headers={"Retry-After": str(window_seconds)},
        )
