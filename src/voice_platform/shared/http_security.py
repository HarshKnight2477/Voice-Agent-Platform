"""HTTP hardening middleware: security headers and an API-wide rate limit.

Both are pure ASGI middleware (no BaseHTTPMiddleware), so streaming responses are unaffected.
"""

from __future__ import annotations

import hashlib
import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from voice_platform.shared.logging import get_logger

log = get_logger(__name__)

_API_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
# JSON APIs never need to load or run anything.
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
_NO_STORE = (b"cache-control", b"no-store")
_HSTS = (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
# The interactive API docs page (non-production only) loads its own scripts.
_DOCS_PATHS = ("/docs", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_API_HEADERS)
                if not path.startswith(_DOCS_PATHS):
                    headers.append(_API_CSP)
                if path.startswith("/api/"):
                    headers.append(_NO_STORE)
                if self.hsts:
                    headers.append(_HSTS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimitMiddleware:
    """Per-client request budget for /api/*. The client is the session (hashed cookie) when
    signed in, otherwise the IP address. Uses app.state.rate_limiter; fails open if unavailable."""

    def __init__(self, app: ASGIApp, *, per_minute: int, cookie_name: str) -> None:
        self.app = app
        self.per_minute = per_minute
        self.cookie_name = cookie_name.encode()

    def _client_key(self, scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                for part in value.split(b";"):
                    k, _, v = part.strip().partition(b"=")
                    if k == self.cookie_name and v:
                        return f"s:{hashlib.sha256(v).hexdigest()[:32]}"
        client = scope.get("client")
        return f"ip:{client[0] if client else 'unknown'}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        app = scope.get("app")
        limiter = getattr(app.state, "rate_limiter", None) if app is not None else None
        if limiter is not None:
            try:
                allowed = await limiter.hit(
                    f"api:{self._client_key(scope)}", limit=self.per_minute, window_seconds=60
                )
            except Exception as exc:
                log.error("rate_limit.unavailable", error=type(exc).__name__)
                allowed = True
            if not allowed:
                await self._reject(scope, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, send: Send) -> None:
        body = json.dumps(
            {
                "type": "https://errors.voice-platform.dev/rate_limited",
                "title": "Too many requests. Please slow down.",
                "status": 429,
                "code": "rate_limited",
                "request_id": scope.get("state", {}).get("request_id"),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"retry-after", b"60"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
