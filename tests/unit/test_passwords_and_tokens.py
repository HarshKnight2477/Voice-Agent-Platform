from __future__ import annotations

import pytest

from voice_platform.identity import passwords
from voice_platform.identity.tokens import csrf_matches, csrf_token_for, hash_token, new_token
from voice_platform.tenancy.permissions import ROLE_PERMISSIONS, ROLE_RANK, Permission, Role


@pytest.mark.parametrize(
    ("pw", "msg"),
    [
        ("short", "at least"),
        ("a" * 129, "at most"),
        ("owner@example.com", "email"),
        ("aaaaaaaaaaaaab", "repetitive"),
    ],
)
def test_password_policy_rejects(pw: str, msg: str) -> None:
    with pytest.raises(passwords.WeakPasswordError, match=msg):
        passwords.check_policy(pw, email="owner@example.com")


def test_password_hash_roundtrip_and_argon2id() -> None:
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password("correct horse battery", h)
    assert not passwords.verify_password("wrong horse battery", h)
    assert not passwords.verify_password("anything", None)
    assert not passwords.verify_password("anything", "not-a-hash")


def test_tokens_are_random_and_hashed() -> None:
    a, b = new_token(), new_token()
    assert a != b and len(a) >= 43
    assert hash_token(a) != a and len(hash_token(a)) == 64


def test_csrf_token_is_bound_to_session() -> None:
    t1, t2 = new_token(), new_token()
    c1 = csrf_token_for(t1, "secret")
    assert csrf_matches(c1, t1, "secret")
    assert not csrf_matches(c1, t2, "secret")
    assert not csrf_matches(c1, t1, "other-secret")


def test_every_role_has_a_rank_and_owner_has_everything() -> None:
    assert set(ROLE_RANK) == set(Role)
    assert ROLE_PERMISSIONS[Role.OWNER] == frozenset(Permission)
    ranks = [ROLE_RANK[r] for r in Role]
    assert ranks == sorted(ranks, reverse=True)
