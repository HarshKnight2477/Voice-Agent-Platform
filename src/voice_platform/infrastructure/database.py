"""Async SQLAlchemy engine. One engine per process, created at startup, disposed at shutdown.

Tenant-scoped sessions (SET LOCAL app.org_id for row-level security) arrive in Phase 1 piece 2.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from voice_platform.shared.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    url = str(settings.database_url).replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(
        url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        pool_timeout=5,
    )
