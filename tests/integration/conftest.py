"""Integration fixtures: a throwaway database migrated from scratch for each test session.

Uses the local Docker PostgreSQL. The dev database is never touched: tests create
`<dbname>_test`, run every migration as the owner, connect as the restricted app role,
and drop the database afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
from api_harness import Harness, make_harness
from sqlalchemy import text
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from voice_platform.cli import configure_app_role, migrate
from voice_platform.infrastructure.database import create_sessionmaker, to_async_url
from voice_platform.shared.config import Settings, get_settings


def _urls() -> tuple[URL, URL, URL]:
    settings = get_settings()
    if settings.migration_database_url is None:
        pytest.skip("MIGRATION_DATABASE_URL not set")
    owner = make_url(to_async_url(str(settings.migration_database_url)))
    app = make_url(to_async_url(str(settings.database_url)))
    test_db = f"{owner.database}_test"
    return owner.set(database="postgres"), owner.set(database=test_db), app.set(database=test_db)


async def _recreate(admin_url: URL, name: str, *, create: bool) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            if create:
                await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database() -> Iterator[tuple[URL, URL]]:
    admin, owner, app = _urls()
    assert owner.database is not None
    asyncio.run(_recreate(admin, owner.database, create=True))
    settings: Settings = get_settings()
    owner_str = owner.render_as_string(hide_password=False)
    migrate(settings, owner_str)
    asyncio.run(configure_app_role(settings, owner_str))
    yield owner, app
    asyncio.run(_recreate(admin, owner.database, create=False))


@pytest.fixture
async def owner_engine(test_database: tuple[URL, URL]) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database[0])
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine(test_database: tuple[URL, URL]) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_database[1])
    yield engine
    await engine.dispose()


@pytest.fixture
def app_sessions(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(app_engine)


@pytest.fixture
async def harness(app_sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    async for h in make_harness(app_sessions):
        yield h
