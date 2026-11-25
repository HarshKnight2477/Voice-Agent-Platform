"""End-to-end auth and permission tests: real PostgreSQL (app role, RLS on), real HTTP layer."""

from __future__ import annotations

import uuid

import pytest
from api_harness import PASSWORD, Harness, csrf, signup, unique_email
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.tenancy.models import Membership
from voice_platform.tenancy.permissions import Role

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------- sign-up and login


async def test_signup_flow(harness: Harness) -> None:
    h = harness
    resp = await signup(h)
    assert resp.status_code == 201, resp.text
    assert "vp_session" in resp.cookies
    cookie_header = resp.headers.get_list("set-cookie")
    assert any(
        "vp_session=" in c and "HttpOnly" in c and "SameSite=lax" in c for c in cookie_header
    )

    me = (await h.client.get("/api/v1/me")).json()
    assert me["role"] == "OWNER"
    assert me["email_verified"] is False
    assert "org.delete" in me["permissions"]
    assert h.mailer.sent[-1].kind == "verify_email"


async def test_signup_rejects_weak_password_and_duplicate_email(harness: Harness) -> None:
    h = harness
    weak = await h.client.post(
        "/api/v1/auth/signup",
        json={
            "email": unique_email(),
            "password": "short",
            "full_name": "x",
            "organization_name": "Org",
        },
    )
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    assert "short" not in weak.text  # submitted values are never echoed

    email = unique_email()
    assert (await signup(h, email)).status_code == 201
    dup = await signup(h, email.upper())  # case-insensitive
    assert dup.status_code == 409 and dup.json()["code"] == "email_taken"


async def test_login_logout_and_wrong_password(harness: Harness) -> None:
    h = harness
    email = unique_email()
    await signup(h, email)
    h.client.cookies.clear()

    bad = await h.client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong-pass-123"}
    )
    unknown = await h.client.post(
        "/api/v1/auth/login", json={"email": unique_email(), "password": PASSWORD}
    )
    assert bad.status_code == unknown.status_code == 401
    assert bad.json()["title"] == unknown.json()["title"]  # no hint which part was wrong

    ok = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert ok.status_code == 200
    assert (await h.client.get("/api/v1/me")).status_code == 200

    out = await h.client.post("/api/v1/auth/logout", headers=csrf(ok))
    assert out.status_code == 204
    assert (await h.client.get("/api/v1/me")).status_code == 401


async def test_login_is_rate_limited(harness: Harness) -> None:
    h = harness
    email = unique_email()
    for _ in range(10):
        await h.client.post(
            "/api/v1/auth/login", json={"email": email, "password": "nope-nope-nope"}
        )
    blocked = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "900"


async def test_auth_endpoints_require_json(harness: Harness) -> None:
    resp = await harness.client.post(
        "/api/v1/auth/login",
        content="email=a&password=b",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 415


# ---------------------------------------------------------------- CSRF and sessions


async def test_state_changes_need_csrf_token(harness: Harness) -> None:
    h = harness
    s = await signup(h)
    no_token = await h.client.patch("/api/v1/organizations/current", json={"name": "New Name"})
    bad_token = await h.client.patch(
        "/api/v1/organizations/current", json={"name": "New Name"}, headers={"x-csrf-token": "x"}
    )
    good = await h.client.patch(
        "/api/v1/organizations/current", json={"name": "New Name"}, headers=csrf(s)
    )
    assert no_token.status_code == bad_token.status_code == 403
    assert no_token.json()["code"] == "csrf_failed"
    assert good.status_code == 200 and good.json()["name"] == "New Name"


async def test_list_and_revoke_sessions(harness: Harness) -> None:
    h = harness
    email = unique_email()
    first = await signup(h, email)
    first_cookie = h.client.cookies["vp_session"]
    h.client.cookies.clear()
    second = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    sessions = (await h.client.get("/api/v1/auth/sessions")).json()
    assert len(sessions) == 2
    other = next(s for s in sessions if not s["current"])
    resp = await h.client.delete(f"/api/v1/auth/sessions/{other['id']}", headers=csrf(second))
    assert resp.status_code == 204

    h.client.cookies.clear()
    h.client.cookies.set("vp_session", first_cookie)
    assert (await h.client.get("/api/v1/me")).status_code == 401
    assert first.status_code == 201


# ---------------------------------------------------------------- email flows


async def test_verify_email_token_is_single_use(harness: Harness) -> None:
    h = harness
    await signup(h)
    token = h.mailer.last_token("verify_email")
    assert (
        await h.client.post("/api/v1/auth/verify-email", json={"token": token})
    ).status_code == 204
    assert (await h.client.get("/api/v1/me")).json()["email_verified"] is True
    again = await h.client.post("/api/v1/auth/verify-email", json={"token": token})
    assert again.status_code == 400 and again.json()["code"] == "invalid_token"


async def test_password_reset_signs_out_everywhere(harness: Harness) -> None:
    h = harness
    email = unique_email()
    await signup(h, email)
    old_cookie = h.client.cookies["vp_session"]
    h.client.cookies.clear()

    unknown = await h.client.post(
        "/api/v1/auth/password-reset/request", json={"email": unique_email()}
    )
    known = await h.client.post("/api/v1/auth/password-reset/request", json={"email": email})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()  # does not reveal which emails exist

    token = h.mailer.last_token("reset_password")
    new_password = "a-brand-new-password-2"
    resp = await h.client.post(
        "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": new_password}
    )
    assert resp.status_code == 204

    h.client.cookies.set("vp_session", old_cookie)
    assert (await h.client.get("/api/v1/me")).status_code == 401  # old session revoked
    h.client.cookies.clear()
    old = await h.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    new = await h.client.post("/api/v1/auth/login", json={"email": email, "password": new_password})
    assert old.status_code == 401 and new.status_code == 200


# ---------------------------------------------------------------- roles and tenants


async def _add_member(
    sessions: async_sessionmaker[AsyncSession], org_id: str, owner_id: str, user_id: str, role: Role
) -> None:
    async with tenant_transaction(
        sessions, org_id=uuid.UUID(org_id), user_id=uuid.UUID(owner_id)
    ) as s:
        s.add(
            Membership(
                organization_id=uuid.UUID(org_id), user_id=uuid.UUID(user_id), role_key=role.value
            )
        )


@pytest.mark.parametrize(
    ("role", "can_read_org", "can_update_org", "can_list_members"),
    [
        (Role.ADMIN, True, True, True),
        (Role.MANAGER, True, False, True),
        (Role.AGENT_MANAGER, True, False, True),
        (Role.ANALYST, True, False, False),
        (Role.VIEWER, True, False, False),
    ],
)
async def test_role_matrix(
    harness: Harness, role: Role, can_read_org: bool, can_update_org: bool, can_list_members: bool
) -> None:
    h = harness
    owner = (await signup(h)).json()
    member_email = unique_email()
    h.client.cookies.clear()
    member = (await signup(h, member_email, org="Member Home Org")).json()
    await _add_member(
        h.sessions, owner["organization_id"], owner["user_id"], member["user_id"], role
    )

    switched = await h.client.post(
        "/api/v1/auth/switch-organization",
        json={"organization_id": owner["organization_id"]},
        headers={"x-csrf-token": member["csrf_token"]},
    )
    assert switched.status_code == 200
    headers = csrf(switched)

    read = await h.client.get("/api/v1/organizations/current")
    update = await h.client.patch(
        "/api/v1/organizations/current", json={"name": "Renamed"}, headers=headers
    )
    members = await h.client.get("/api/v1/organizations/current/members")
    assert (read.status_code == 200) is can_read_org
    assert (update.status_code == 200) is can_update_org
    assert (members.status_code == 200) is can_list_members
    if not can_update_org:
        assert update.status_code == 403 and update.json()["code"] == "forbidden"


async def test_cannot_switch_into_an_organization_you_do_not_belong_to(harness: Harness) -> None:
    h = harness
    victim = (await signup(h, org="Victim Org")).json()
    h.client.cookies.clear()
    attacker = (await signup(h, org="Attacker Org")).json()

    resp = await h.client.post(
        "/api/v1/auth/switch-organization",
        json={"organization_id": victim["organization_id"]},
        headers={"x-csrf-token": attacker["csrf_token"]},
    )
    assert resp.status_code == 404
    current = (await h.client.get("/api/v1/organizations/current")).json()
    assert current["id"] == attacker["organization_id"]
    members = (await h.client.get("/api/v1/organizations/current/members")).json()
    assert [m["user_id"] for m in members["data"]] == [attacker["user_id"]]


async def test_removed_member_loses_access_immediately(
    harness: Harness, owner_engine: AsyncEngine
) -> None:
    h = harness
    owner = (await signup(h)).json()
    h.client.cookies.clear()
    member = (await signup(h, org="Elsewhere")).json()
    await _add_member(
        h.sessions, owner["organization_id"], owner["user_id"], member["user_id"], Role.VIEWER
    )
    await h.client.post(
        "/api/v1/auth/switch-organization",
        json={"organization_id": owner["organization_id"]},
        headers={"x-csrf-token": member["csrf_token"]},
    )
    assert (await h.client.get("/api/v1/organizations/current")).status_code == 200

    async with owner_engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM memberships WHERE organization_id = :o AND user_id = :u"),
            {"o": owner["organization_id"], "u": member["user_id"]},
        )
    resp = await h.client.get("/api/v1/organizations/current")
    assert resp.status_code == 403 and resp.json()["code"] == "not_a_member"


async def test_signup_writes_audit_and_outbox(harness: Harness, owner_engine: AsyncEngine) -> None:
    h = harness
    org_id = (await signup(h)).json()["organization_id"]
    async with owner_engine.connect() as conn:
        actions = set(
            (
                await conn.scalars(
                    select(text("action"))
                    .select_from(text("audit_logs"))
                    .where(text("organization_id = :o")),
                    {"o": org_id},
                )
            ).all()
        )
        events = (
            await conn.scalars(
                text("SELECT event_type FROM outbox_events WHERE organization_id = :o"),
                {"o": org_id},
            )
        ).all()
    assert {"user.signed_up", "organization.created"} <= actions
    assert list(events) == ["organization.created"]
