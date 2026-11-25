"""Control-plane API application factory.

Run locally:  uv run uvicorn voice_platform.api.main:create_app --factory --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from voice_platform import __version__
from voice_platform.api import auth, health, members, organizations
from voice_platform.api.dependencies import CSRF_HEADER, SESSION_COOKIE
from voice_platform.api.errors import install_error_handlers
from voice_platform.identity.email import LocalDevEmailSender
from voice_platform.infrastructure.database import create_engine, create_sessionmaker
from voice_platform.infrastructure.health import LiveDependencyChecker
from voice_platform.infrastructure.redis import create_redis
from voice_platform.shared.config import Environment, Settings, get_settings
from voice_platform.shared.http_security import RateLimitMiddleware, SecurityHeadersMiddleware
from voice_platform.shared.logging import configure_logging, get_logger
from voice_platform.shared.rate_limit import RedisRateLimiter
from voice_platform.shared.request_context import REQUEST_ID_HEADER, RequestContextMiddleware

log = get_logger(__name__)


def build_routes(app: FastAPI) -> None:
    """Routers and error handlers, shared by the real app and the test app."""
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(organizations.router)
    app.include_router(members.router)


def install_middleware(app: FastAPI, settings: Settings) -> None:
    """Outermost first in effect: request id -> security headers -> CORS -> rate limit."""
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.api_rate_limit_per_minute,
        cookie_name=SESSION_COOKIE,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["content-type", CSRF_HEADER, REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER, "retry-after"],
        max_age=600,
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.app_env in (Environment.STAGING, Environment.PRODUCTION),
    )
    app.add_middleware(RequestContextMiddleware)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        redis = create_redis(settings)
        app.state.sessionmaker = create_sessionmaker(engine)
        app.state.dependency_checker = LiveDependencyChecker(engine, redis)
        app.state.rate_limiter = RedisRateLimiter(redis)
        # A real email provider replaces this before staging (it refuses to run there).
        app.state.email_sender = LocalDevEmailSender(settings)
        log.info("app.started", env=settings.app_env.value, version=__version__)
        try:
            yield
        finally:
            await redis.aclose()
            await engine.dispose()
            log.info("app.stopped")

    app = FastAPI(
        title="Voice Agent Platform API",
        version=__version__,
        lifespan=lifespan,
        # Interactive docs only outside production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    install_middleware(app, settings)
    build_routes(app)
    return app
