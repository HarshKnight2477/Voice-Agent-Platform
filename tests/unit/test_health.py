from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from voice_platform.api import health
from voice_platform.infrastructure.health import get_dependency_checker
from voice_platform.shared.request_context import RequestContextMiddleware


class FakeChecker:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()

    async def check(self, name: str) -> None:
        if name in self.failing:
            raise ConnectionError("down")


def make_app(checker: FakeChecker) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.dependency_overrides[get_dependency_checker] = lambda: checker
    return app


async def get(app: FastAPI, path: str, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=headers)


async def test_live_is_ok_without_dependencies() -> None:
    resp = await get(make_app(FakeChecker(failing={"database", "redis"})), "/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_ready_ok_when_all_dependencies_answer() -> None:
    resp = await get(make_app(FakeChecker()), "/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.parametrize("down", ["database", "redis"])
async def test_ready_returns_503_and_hides_error_details(down: str) -> None:
    resp = await get(make_app(FakeChecker(failing={down})), "/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["checks"][down] == "unavailable"
    assert "down" not in resp.text  # exception text is logged, never returned


async def test_request_id_is_generated_and_returned() -> None:
    resp = await get(make_app(FakeChecker()), "/health/live")
    assert len(resp.headers["x-request-id"]) == 32


async def test_safe_incoming_request_id_is_kept() -> None:
    resp = await get(make_app(FakeChecker()), "/health/live", {"x-request-id": "abc-12345678"})
    assert resp.headers["x-request-id"] == "abc-12345678"


async def test_unsafe_incoming_request_id_is_replaced() -> None:
    resp = await get(
        make_app(FakeChecker()), "/health/live", {"x-request-id": "bad id\nwith newline"}
    )
    assert resp.headers["x-request-id"] != "bad id\nwith newline"
    assert len(resp.headers["x-request-id"]) == 32
