"""Random tokens for sessions and emails, and the CSRF token derived from a session token."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    """256 bits of randomness, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are high-entropy, so a fast hash is enough and lets us look them up by hash."""
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token_for(session_token: str, secret_key: str) -> str:
    """Bound to the session: a stolen CSRF value is useless without that session's cookie."""
    return hmac.new(secret_key.encode(), f"csrf:{session_token}".encode(), "sha256").hexdigest()


def csrf_matches(provided: str, session_token: str, secret_key: str) -> bool:
    return hmac.compare_digest(provided, csrf_token_for(session_token, secret_key))
