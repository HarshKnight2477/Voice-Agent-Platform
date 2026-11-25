"""Control-plane API application factory.

Run locally:  uv run uvicorn voice_platform.api.main:create_app --factory --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from voice_platform import __version__
from voice_platform.api import health
from voice_platform.infrastructure.database import create_engine
from voice_platform.infrastructure.health import LiveDependencyChecker
from voice_platform.infrastructure.redis import create_redis
from voice_platform.shared.config import Settings, get_settings
from voice_platform.shared.logging import configure_logging, get_logger
from voice_platform.shared.request_context import RequestContextMiddleware

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        redis = create_redis(settings)
        app.state.dependency_checker = LiveDependencyChecker(engine, redis)
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
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    return app
