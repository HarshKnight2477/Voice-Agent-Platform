"""Operational commands.

  uv run python -m voice_platform.cli db-setup     migrate to the latest schema, then give the
                                                   runtime role LOGIN and its password
  uv run python -m voice_platform.cli db-migrate   migrate only

Both connect with MIGRATION_DATABASE_URL (the owner role). The API never uses that URL.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from voice_platform.infrastructure.database import to_async_url
from voice_platform.infrastructure.db_base import APP_DB_ROLE
from voice_platform.shared.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[2]


def _owner_url(settings: Settings) -> str:
    if settings.migration_database_url is None:
        raise SystemExit("MIGRATION_DATABASE_URL is not set")
    return to_async_url(str(settings.migration_database_url))


def migrate(settings: Settings, url: str | None = None) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = url or _owner_url(settings)
    command.upgrade(cfg, "head")


async def configure_app_role(settings: Settings, url: str | None = None) -> None:
    if settings.app_db_password is None:
        raise SystemExit("APP_DB_PASSWORD is not set")
    owner_url = url or _owner_url(settings)
    db_name = make_url(owner_url).database
    engine = create_async_engine(owner_url)
    try:
        async with engine.begin() as conn:
            # The password travels as a bound parameter, then format(%L) quotes it server-side.
            await conn.execute(
                text("SELECT set_config('vp.app_pw', :pw, true)"),
                {"pw": settings.app_db_password.get_secret_value()},
            )
            await conn.execute(
                text(
                    f"""
                    DO $$ BEGIN
                      EXECUTE format('ALTER ROLE {APP_DB_ROLE} LOGIN PASSWORD %L',
                                     current_setting('vp.app_pw'));
                    END $$;
                    """
                )
            )
            await conn.execute(text(f'GRANT CONNECT ON DATABASE "{db_name}" TO {APP_DB_ROLE}'))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="voice_platform.cli")
    parser.add_argument("command", choices=["db-setup", "db-migrate"])
    args = parser.parse_args()
    settings = get_settings()
    migrate(settings)
    if args.command == "db-setup":
        asyncio.run(configure_app_role(settings))
    print(f"{args.command}: done")


if __name__ == "__main__":
    main()
