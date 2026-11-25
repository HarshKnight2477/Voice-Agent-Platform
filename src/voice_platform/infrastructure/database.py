"""Async SQLAlchemy engine and tenant-scoped transactions.

Row-level security reads two transaction-local settings:
  app.org_id  - the organization the request acts for (from the authenticated session)
  app.user_id - the signed-in user (lets a user see their own memberships across orgs)
Both are set with set_config(..., is_local => true), so they vanish at commit/rollback and
can never leak to the next request that reuses the pooled connection.
With neither set, tenant tables return no rows: the default is closed, not open.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from voice_platform.shared.config import Settings


def to_async_url(url: str) -> str:
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        to_async_url(str(settings.database_url)),
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        pool_timeout=5,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_transaction(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> AsyncIterator[AsyncSession]:
    """One database transaction acting for (org_id, user_id).

    org_id and user_id must come from server-side authentication, never from request input.
    Commits on success, rolls back on any exception.
    """
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "SELECT set_config('app.org_id', :org, true), set_config('app.user_id', :usr, true)"
            ),
            {"org": str(org_id) if org_id else "", "usr": str(user_id) if user_id else ""},
        )
        yield session
