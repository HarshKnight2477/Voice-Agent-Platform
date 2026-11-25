"""/api/v1/auth: sign-up, login, logout, organization switching, sessions, email flows."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from voice_platform.api.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AppSettings,
    CurrentSession,
    Meta,
    SessionMaker,
    get_email_sender,
    get_rate_limiter,
    rate_limit,
    require_json_body,
)
from voice_platform.api.errors import AppError
from voice_platform.identity import service
from voice_platform.identity.email import AccountEmail, EmailSender
from voice_platform.identity.passwords import MAX_LENGTH, WeakPasswordError
from voice_platform.identity.tokens import csrf_token_for, hash_token
from voice_platform.shared.config import Settings
from voice_platform.shared.rate_limit import RateLimiter

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]
Mailer = Annotated[EmailSender, Depends(get_email_sender)]
JsonOnly = Depends(require_json_body)


# ---------------------------------------------------------------- schemas


class SignupRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_LENGTH)
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=2, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=MAX_LENGTH)


class SessionResponse(BaseModel):
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    csrf_token: str


class SwitchOrganizationRequest(BaseModel):
    organization_id: uuid.UUID


class TokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class PasswordResetRequest(BaseModel):
    email: str = Field(max_length=254)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(max_length=MAX_LENGTH)


class SessionInfo(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    user_agent: str | None
    current: bool


# ---------------------------------------------------------------- helpers


def _ttl(settings: Settings) -> timedelta:
    return timedelta(hours=settings.session_ttl_hours)


def _set_session_cookies(response: Response, token: str, settings: Settings) -> str:
    max_age = settings.session_ttl_hours * 3600
    csrf = csrf_token_for(token, settings.app_secret_key.get_secret_value())
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    # Readable by the dashboard's JavaScript so it can send X-CSRF-Token.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return csrf


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, samesite="lax")


def _client_key(meta: service.RequestMeta) -> str:
    return meta.ip or "unknown"


# ---------------------------------------------------------------- endpoints


@router.post("/signup", status_code=status.HTTP_201_CREATED, dependencies=[JsonOnly])
async def signup(
    body: SignupRequest,
    response: Response,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
    limiter: Limiter,
    mailer: Mailer,
) -> SessionResponse:
    await rate_limit(limiter, f"signup:ip:{_client_key(meta)}", limit=5, window_seconds=3600)
    try:
        result = await service.signup(
            sm,
            email=body.email,
            password=body.password,
            full_name=body.full_name,
            organization_name=body.organization_name,
            meta=meta,
            session_ttl=_ttl(settings),
        )
    except service.InvalidEmailError:
        raise AppError(422, "invalid_email", "That email address isn't valid.") from None
    except WeakPasswordError as exc:
        raise AppError(422, "weak_password", str(exc)) from None
    except service.EmailTakenError:
        raise AppError(409, "email_taken", "An account with that email already exists.") from None

    await mailer.send(
        AccountEmail(
            to=result.email,
            kind="verify_email",
            link=f"{settings.public_app_url}/verify-email?token={result.verify_token}",
        )
    )
    csrf = _set_session_cookies(response, result.session.token, settings)
    return SessionResponse(
        user_id=result.session.user_id,
        organization_id=result.session.organization_id,
        csrf_token=csrf,
    )


@router.post("/login", dependencies=[JsonOnly])
async def login(
    body: LoginRequest,
    response: Response,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
    limiter: Limiter,
) -> SessionResponse:
    email_key = body.email.strip().lower()
    await rate_limit(limiter, f"login:ip:{_client_key(meta)}", limit=50, window_seconds=900)
    await rate_limit(limiter, f"login:email:{hash_token(email_key)}", limit=10, window_seconds=900)
    try:
        session = await service.login(
            sm, email=body.email, password=body.password, meta=meta, session_ttl=_ttl(settings)
        )
    except service.InvalidCredentialsError:
        raise AppError(401, "invalid_credentials", "Email or password is incorrect.") from None
    csrf = _set_session_cookies(response, session.token, settings)
    return SessionResponse(
        user_id=session.user_id, organization_id=session.organization_id, csrf_token=csrf
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: CurrentSession,
    response: Response,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
) -> None:
    await service.logout(sm, session, meta)
    _clear_session_cookies(response, settings)


@router.post("/switch-organization")
async def switch_organization(
    body: SwitchOrganizationRequest,
    session: CurrentSession,
    response: Response,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
) -> SessionResponse:
    try:
        new = await service.switch_organization(
            sm, session, body.organization_id, meta, _ttl(settings)
        )
    except service.NotAMemberError:
        # Same answer whether the organization exists or not.
        raise AppError(404, "not_found", "Organization not found.") from None
    csrf = _set_session_cookies(response, new.token, settings)
    return SessionResponse(
        user_id=new.user_id, organization_id=new.organization_id, csrf_token=csrf
    )


@router.get("/sessions")
async def list_sessions(session: CurrentSession, sm: SessionMaker) -> list[SessionInfo]:
    rows = await service.list_sessions(sm, session.user_id)
    return [
        SessionInfo(
            id=r.id,
            created_at=r.created_at,
            last_seen_at=r.last_seen_at,
            user_agent=r.user_agent,
            current=r.id == session.session_id,
        )
        for r in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, session: CurrentSession, sm: SessionMaker, meta: Meta
) -> None:
    if not await service.revoke_session(sm, session.user_id, session_id, meta):
        raise AppError(404, "not_found", "Session not found.")


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT, dependencies=[JsonOnly])
async def verify_email(body: TokenRequest, sm: SessionMaker, meta: Meta, limiter: Limiter) -> None:
    await rate_limit(limiter, f"verify:ip:{_client_key(meta)}", limit=20, window_seconds=3600)
    try:
        await service.verify_email(sm, body.token, meta)
    except service.InvalidTokenError:
        raise AppError(400, "invalid_token", "This link is invalid or has expired.") from None


@router.post(
    "/password-reset/request", status_code=status.HTTP_202_ACCEPTED, dependencies=[JsonOnly]
)
async def request_password_reset(
    body: PasswordResetRequest,
    request: Request,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
    limiter: Limiter,
    mailer: Mailer,
) -> dict[str, str]:
    await rate_limit(limiter, f"reset:ip:{_client_key(meta)}", limit=5, window_seconds=3600)
    await rate_limit(
        limiter,
        f"reset:email:{hash_token(body.email.strip().lower())}",
        limit=3,
        window_seconds=3600,
    )
    result = await service.request_password_reset(sm, body.email, meta)
    if result is not None:
        email, token = result
        await mailer.send(
            AccountEmail(
                to=email,
                kind="reset_password",
                link=f"{settings.public_app_url}/reset-password?token={token}",
            )
        )
    # Identical response whether or not the account exists.
    return {"status": "If that email has an account, a reset link is on its way."}


@router.post(
    "/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT, dependencies=[JsonOnly]
)
async def confirm_password_reset(
    body: PasswordResetConfirm,
    response: Response,
    sm: SessionMaker,
    settings: AppSettings,
    meta: Meta,
    limiter: Limiter,
) -> None:
    await rate_limit(
        limiter, f"reset-confirm:ip:{_client_key(meta)}", limit=10, window_seconds=3600
    )
    try:
        await service.reset_password(sm, body.token, body.new_password, meta)
    except service.InvalidTokenError:
        raise AppError(400, "invalid_token", "This link is invalid or has expired.") from None
    except WeakPasswordError as exc:
        raise AppError(422, "weak_password", str(exc)) from None
    _clear_session_cookies(response, settings)
