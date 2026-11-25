"""Invitations, role changes, removal, pagination and HTTP hardening, end to end."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_harness import PASSWORD, Harness, csrf, make_harness, signup, unique_email
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration


async def login(h: Harness, email: str) -> Response:
    h.client.cookies.clear()
    resp = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp


async def invite_and_join(h: Harness, owner_email: str, role: str) -> tuple[str, str]:
    """Owner invites a new person with `role`; they sign up and accept. Returns (email, org_id)."""
    owner = await login(h, owner_email)
    org_id = owner.json()["organization_id"]
    email = unique_email()
    resp = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": email, "role": role},
        headers=csrf(owner),
    )
    assert resp.status_code == 201, resp.text
    token = h.mailer.last_token("invitation")

    h.client.cookies.clear()
    joined = await signup(h, email, org="Their Own Org")
    accepted = await h.client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=csrf(joined)
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"organization_id": org_id, "role": role}
    return email, org_id


async def act_in(h: Harness, email: str, org_id: str) -> dict[str, str]:
    resp = await login(h, email)
    sw = await h.client.post(
        "/api/v1/auth/switch-organization", json={"organization_id": org_id}, headers=csrf(resp)
    )
    assert sw.status_code == 200, sw.text
    return csrf(sw)


async def member_id(h: Harness, email: str) -> str:
    members = (await h.client.get("/api/v1/organizations/current/members")).json()["data"]
    return next(m["user_id"] for m in members if m["email"] == email)


# ---------------------------------------------------------------- invitations


async def test_invitation_flow(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    await signup(h, owner_email)
    email, org_id = await invite_and_join(h, owner_email, "AGENT_MANAGER")

    await act_in(h, email, org_id)
    me = (await h.client.get("/api/v1/me")).json()
    assert me["role"] == "AGENT_MANAGER"
    assert me["email_verified"] is True  # accepting the emailed link proves the address
    assert {m["organization_id"] for m in me["memberships"]} >= {org_id}


async def test_invitation_rejects_wrong_account_and_reuse(harness: Harness) -> None:
    h = harness
    owner = await signup(h)
    resp = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": unique_email(), "role": "VIEWER"},
        headers=csrf(owner),
    )
    assert resp.status_code == 201
    token = h.mailer.last_token("invitation")

    h.client.cookies.clear()
    stranger = await signup(h)  # different email than the invitation
    wrong = await h.client.post(
        "/api/v1/invitations/accept", json={"token": token}, headers=csrf(stranger)
    )
    assert wrong.status_code == 403 and wrong.json()["code"] == "wrong_account"

    forged = await h.client.post(
        "/api/v1/invitations/accept",
        json={"token": token[:-4] + "AAAA"},
        headers=csrf(stranger),
    )
    assert forged.status_code == 400


async def test_cannot_invite_above_own_role_or_duplicate(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    await signup(h, owner_email)
    mgr_email, org_id = await invite_and_join(h, owner_email, "MANAGER")
    headers = await act_in(h, mgr_email, org_id)

    too_high = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": unique_email(), "role": "ADMIN"},
        headers=headers,
    )
    assert too_high.status_code == 403 and too_high.json()["code"] == "role_too_high"

    target = unique_email()
    first = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": target, "role": "VIEWER"},
        headers=headers,
    )
    dup = await h.client.post(
        "/api/v1/organizations/current/invitations",
        json={"email": target, "role": "VIEWER"},
        headers=headers,
    )
    assert first.status_code == 201
    assert dup.status_code == 409 and dup.json()["code"] == "invitation_exists"

    revoke = await h.client.delete(
        f"/api/v1/organizations/current/invitations/{first.json()['id']}", headers=headers
    )
    assert revoke.status_code == 204
    pending = (await h.client.get("/api/v1/organizations/current/invitations")).json()
    assert target not in [i["email"] for i in pending]


# ---------------------------------------------------------------- roles and removal


async def test_role_changes_follow_rank_rules(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    owner = await signup(h, owner_email)
    analyst_email, org_id = await invite_and_join(h, owner_email, "ANALYST")

    headers = csrf(await login(h, owner_email))
    analyst_id = await member_id(h, analyst_email)
    promote = await h.client.patch(
        f"/api/v1/organizations/current/members/{analyst_id}",
        json={"role": "MANAGER"},
        headers=headers,
    )
    assert promote.status_code == 200 and promote.json()["role"] == "MANAGER"

    self_change = await h.client.patch(
        f"/api/v1/organizations/current/members/{owner.json()['user_id']}",
        json={"role": "ADMIN"},
        headers=headers,
    )
    assert self_change.status_code == 403
    assert self_change.json()["code"] == "cannot_change_own_role"

    # The manager cannot touch the owner.
    mgr_headers = await act_in(h, analyst_email, org_id)
    demote_owner = await h.client.patch(
        f"/api/v1/organizations/current/members/{owner.json()['user_id']}",
        json={"role": "VIEWER"},
        headers=mgr_headers,
    )
    assert demote_owner.status_code == 403 and demote_owner.json()["code"] == "target_too_high"


async def test_last_owner_cannot_leave_and_removed_member_loses_access(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    owner = await signup(h, owner_email)
    viewer_email, org_id = await invite_and_join(h, owner_email, "VIEWER")

    headers = csrf(await login(h, owner_email))
    leave = await h.client.delete(
        f"/api/v1/organizations/current/members/{owner.json()['user_id']}", headers=headers
    )
    assert leave.status_code == 403 and leave.json()["code"] == "last_owner"

    viewer_headers = await act_in(h, viewer_email, org_id)
    viewer_cookie = h.client.cookies["vp_session"]
    cannot_remove = await h.client.delete(
        f"/api/v1/organizations/current/members/{owner.json()['user_id']}", headers=viewer_headers
    )
    assert cannot_remove.status_code == 403

    headers = csrf(await login(h, owner_email))
    viewer_id = await member_id(h, viewer_email)
    removed = await h.client.delete(
        f"/api/v1/organizations/current/members/{viewer_id}", headers=headers
    )
    assert removed.status_code == 204

    h.client.cookies.clear()
    h.client.cookies.set("vp_session", viewer_cookie)
    after = await h.client.get("/api/v1/organizations/current")
    assert after.status_code == 403 and after.json()["code"] == "no_organization"


# ---------------------------------------------------------------- pagination and audit


async def test_audit_log_pagination(harness: Harness) -> None:
    h = harness
    owner = await signup(h)
    headers = csrf(owner)
    for i in range(5):
        await h.client.patch(
            "/api/v1/organizations/current", json={"name": f"Name {i}"}, headers=headers
        )

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2, "action": "organization.updated"}
        if cursor:
            params["cursor"] = cursor
        page = (
            await h.client.get("/api/v1/organizations/current/audit-logs", params=params)
        ).json()
        seen += [row["id"] for row in page["data"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 5

    bad = await h.client.get("/api/v1/organizations/current/audit-logs", params={"cursor": "%%%"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"
    too_big = await h.client.get("/api/v1/organizations/current/audit-logs", params={"limit": 1000})
    assert too_big.status_code == 422


async def test_viewer_cannot_read_audit_log(harness: Harness) -> None:
    h = harness
    owner_email = unique_email()
    await signup(h, owner_email)
    viewer_email, org_id = await invite_and_join(h, owner_email, "VIEWER")
    await act_in(h, viewer_email, org_id)
    resp = await h.client.get("/api/v1/organizations/current/audit-logs")
    assert resp.status_code == 403


# ---------------------------------------------------------------- HTTP hardening


async def test_security_headers_and_cors(harness: Harness) -> None:
    h = harness
    resp = await h.client.get("/api/v1/me")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"

    allowed = await h.client.options(
        "/api/v1/auth/login",
        headers={
            "origin": "http://dashboard.test",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type,x-csrf-token",
        },
    )
    assert allowed.headers["access-control-allow-origin"] == "http://dashboard.test"
    assert allowed.headers["access-control-allow-credentials"] == "true"

    evil = await h.client.options(
        "/api/v1/auth/login",
        headers={"origin": "https://evil.example", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in evil.headers


@pytest.fixture
async def tight_harness(app_sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    async for h in make_harness(app_sessions, api_rate_limit_per_minute=10):
        yield h


async def test_api_rate_limit(tight_harness: Harness) -> None:
    h = tight_harness
    codes = [(await h.client.get("/api/v1/me")).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]
    limited = await h.client.get("/api/v1/me")
    assert limited.json()["code"] == "rate_limited" and limited.headers["retry-after"] == "60"
    assert (await h.client.get("/health/live")).status_code == 200  # health is never limited
