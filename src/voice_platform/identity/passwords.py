"""Password hashing (Argon2id) and the password policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128  # bounds hashing cost and blocks oversized-input abuse

_hasher = PasswordHasher()  # library defaults follow the current OWASP Argon2id guidance
# Verified against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password-used-for-timing-only")


class WeakPasswordError(ValueError):
    pass


def check_policy(password: str, *, email: str) -> None:
    if len(password) < MIN_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise WeakPasswordError(f"Password must be at most {MAX_LENGTH} characters.")
    if password.strip().lower() == email.strip().lower():
        raise WeakPasswordError("Password must not be your email address.")
    if len(set(password)) < 4:
        raise WeakPasswordError("Password is too repetitive.")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
