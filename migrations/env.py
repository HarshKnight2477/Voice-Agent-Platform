"""Alembic environment. Runs migrations as the owner role from MIGRATION_DATABASE_URL."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from voice_platform.infrastructure.database import to_async_url
from voice_platform.infrastructure.models_registry import metadata
from voice_platform.shared.config import get_settings

target_metadata = metadata


def _url() -> str:
    override = context.config.attributes.get("database_url")
    if override:
        return str(override)
    settings = get_settings()
    if settings.migration_database_url is None:
        raise RuntimeError("MIGRATION_DATABASE_URL is not set")
    return to_async_url(str(settings.migration_database_url))


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not supported; run against a database.")

asyncio.run(_run_async())
