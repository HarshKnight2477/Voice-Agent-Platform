"""Runs against the real Docker services. Start them first:  docker compose up -d"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from voice_platform.api.main import create_app
from voice_platform.infrastructure.database import create_engine
from voice_platform.shared.config import get_settings

pytestmark = pytest.mark.integration


async def test_ready_endpoint_against_real_services() -> None:
    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_required_postgres_extensions_installed() -> None:
    engine = create_engine(get_settings())
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT extname FROM pg_extension"))
            names = {r[0] for r in rows}
    finally:
        await engine.dispose()
    assert {"vector", "pgcrypto", "citext"} <= names
