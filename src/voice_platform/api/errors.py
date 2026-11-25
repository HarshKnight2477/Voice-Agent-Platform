"""Consistent error responses (RFC 9457 problem+json).

Internal details are logged, never returned.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from voice_platform.shared.logging import get_logger

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class AppError(Exception):
    """An expected, client-facing error. `code` is stable and machine-readable."""

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = headers


def _problem(
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://errors.voice-platform.dev/{code}",
        "title": title,
        "status": status,
        "code": code,
        "request_id": request.scope.get("state", {}).get("request_id"),
    }
    if detail:
        body["detail"] = detail
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        return _problem(request, exc.status, exc.code, exc.title, exc.detail, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo field locations and messages, never the submitted values (could be passwords).
        errors = [
            {"field": ".".join(str(p) for p in e.get("loc", ())[1:]), "message": e.get("msg", "")}
            for e in exc.errors()
        ]
        return _problem(
            request, 422, "validation_error", "Some fields are invalid.", extra={"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _problem(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_error", error=type(exc).__name__)
        return _problem(request, 500, "internal_error", "Something went wrong on our side.")
