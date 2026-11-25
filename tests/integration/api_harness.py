"""Shared HTTP test harness: the real app routes and middleware, a test database connection
as the restricted app role, and fakes for email and rate limiting."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.api.main import build_routes, install_middleware
from voice_platform.identity.email import AccountEmail
from voice_platform.shared.config import Environment, Settings, get_settings

PASSWORD = "a-long-test-password-1"


@dataclass
class FakeMailer:
    sent: list[AccountEmail] = field(default_factory=list)

    async def send(self, email: AccountEmail) -> None:
        self.sent.append(email)

    def last_token(self, kind: str) -> str:
        email = next(e for e in reversed(self.sent) if e.kind == kind)
        match = re.search(r"token=([^&]+)", email.link)
        assert match
        return match.group(1)


class FakeLimiter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> bool:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key] <= limit


@dataclass
class Harness:
    client: AsyncClient
    mailer: FakeMailer
    limiter: FakeLimiter
    sessions: async_sessionmaker[AsyncSession]


async def make_harness(
    app_sessions: async_sessionmaker[AsyncSession], **overrides: object
) -> AsyncIterator[Harness]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=Environment.TEST,
        database_url="postgresql://unused:unused@127.0.0.1/unused",  # type: ignore[arg-type]
        redis_url="redis://127.0.0.1:6379/0",  # type: ignore[arg-type]
        app_secret_key=SecretStr("test-secret-key"),
        cookie_secure=False,
        public_app_url="http://dashboard.test",
        api_rate_limit_per_minute=10_000,
    ).model_copy(update=overrides)
    app = FastAPI()
    install_middleware(app, settings)
    build_routes(app)
    mailer, limiter = FakeMailer(), FakeLimiter()
    app.state.sessionmaker = app_sessions
    app.state.email_sender = mailer
    app.state.rate_limiter = limiter
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Harness(client, mailer, limiter, app_sessions)


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


async def signup(h: Harness, email: str | None = None, org: str = "Pizza Place") -> Response:
    return await h.client.post(
        "/api/v1/auth/signup",
        json={
            "email": email or unique_email(),
            "password": PASSWORD,
            "full_name": "Test Owner",
            "organization_name": org,
        },
    )


def csrf(resp: Response) -> dict[str, str]:
    return {"x-csrf-token": resp.json()["csrf_token"]}
