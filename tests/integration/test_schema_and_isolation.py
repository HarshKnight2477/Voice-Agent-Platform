"""Schema, migrations and tenant isolation against a real PostgreSQL.

The isolation tests connect as the runtime role (voice_app), exactly as the API will.
"""

from __future__ import annotations

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from voice_platform.audit.models import AuditLog
from voice_platform.identity.models import User
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.infrastructure.models_registry import metadata
from voice_platform.tenancy.models import Membership, Organization
from voice_platform.tenancy.permissions import ROLE_PERMISSIONS, Permission, Role

pytestmark = pytest.mark.integration


# ---------- helpers ----------


async def create_org_with_owner(
    sessions: async_sessionmaker[AsyncSession], slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """Signup-shaped flow: user first (global), then org + membership inside that org's context."""
    user_id, org_id = uuid.uuid4(), uuid.uuid4()
    async with tenant_transaction(sessions, org_id=None, user_id=None) as s:
        s.add(User(id=user_id, email=f"{slug}@example.com", full_name=slug))
    async with tenant_transaction(sessions, org_id=org_id, user_id=user_id) as s:
        s.add(Organization(id=org_id, name=slug.title(), slug=slug))
        await s.flush()
        s.add(Membership(organization_id=org_id, user_id=user_id, role_key=Role.OWNER.value))
    return org_id, user_id


# ---------- schema ----------


async def test_models_match_migrations(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), metadata)
        )
    assert diff == [], f"models and migrations differ: {diff}"


async def test_role_catalog_matches_code(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT role_key, permission_key FROM role_permissions"))
        ).all()
    in_db: dict[str, set[str]] = {}
    for role, perm in rows:
        in_db.setdefault(role, set()).add(perm)
    in_code = {r.value: {p.value for p in perms} for r, perms in ROLE_PERMISSIONS.items()}
    assert in_db == in_code


def test_permissions_do_not_imply_each_other() -> None:
    viewer = ROLE_PERMISSIONS[Role.VIEWER]
    assert Permission.CALLS_READ in viewer
    assert Permission.CALLS_TRANSCRIPT_READ not in viewer
    assert Permission.ORG_DELETE not in ROLE_PERMISSIONS[Role.ADMIN]


async def test_app_role_cannot_bypass_rls(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert row == (False, False)


# ---------- tenant isolation ----------


async def test_org_context_sees_only_its_own_rows(
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_a, user_a = await create_org_with_owner(app_sessions, f"a-{uuid.uuid4().hex[:8]}")
    org_b, _ = await create_org_with_owner(app_sessions, f"b-{uuid.uuid4().hex[:8]}")

    async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
        orgs = set((await s.scalars(select(Organization.id))).all())
        member_orgs = set((await s.scalars(select(Membership.organization_id))).all())
    assert org_a in orgs and org_b not in orgs
    assert member_orgs == {org_a}


async def test_no_context_sees_nothing(app_sessions: async_sessionmaker[AsyncSession]) -> None:
    await create_org_with_owner(app_sessions, f"c-{uuid.uuid4().hex[:8]}")
    async with tenant_transaction(app_sessions, org_id=None, user_id=None) as s:
        assert await s.scalar(select(func.count()).select_from(Organization)) == 0
        assert await s.scalar(select(func.count()).select_from(Membership)) == 0


async def test_user_sees_own_memberships_across_orgs_but_not_other_members(
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_a, user_a = await create_org_with_owner(app_sessions, f"d-{uuid.uuid4().hex[:8]}")
    org_b, user_b = await create_org_with_owner(app_sessions, f"e-{uuid.uuid4().hex[:8]}")
    # user_a also joins org_b as a viewer (added by org_b's context).
    async with tenant_transaction(app_sessions, org_id=org_b, user_id=user_b) as s:
        s.add(Membership(organization_id=org_b, user_id=user_a, role_key=Role.VIEWER.value))

    # With only the user context (e.g. an org picker after login):
    async with tenant_transaction(app_sessions, org_id=None, user_id=user_a) as s:
        rows = (await s.execute(select(Membership.organization_id, Membership.user_id))).all()
        orgs = set((await s.scalars(select(Organization.id))).all())
    assert {(org_a, user_a), (org_b, user_a)} == set(rows)  # never user_b's membership
    assert orgs == {org_a, org_b}


async def test_cannot_write_into_another_org(
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_a, user_a = await create_org_with_owner(app_sessions, f"f-{uuid.uuid4().hex[:8]}")
    org_b, _ = await create_org_with_owner(app_sessions, f"g-{uuid.uuid4().hex[:8]}")

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
            s.add(Membership(organization_id=org_b, user_id=user_a, role_key="ADMIN"))

    async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
        result = await s.execute(
            text("UPDATE organizations SET name = 'hijacked' WHERE id = :id"), {"id": org_b}
        )
    assert result.rowcount == 0  # type: ignore[attr-defined]


async def test_context_does_not_leak_between_transactions(
    app_sessions: async_sessionmaker[AsyncSession],
) -> None:
    org_a, user_a = await create_org_with_owner(app_sessions, f"h-{uuid.uuid4().hex[:8]}")
    async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
        assert await s.scalar(select(func.count()).select_from(Organization)) == 1
    async with app_sessions() as s, s.begin():  # a later transaction on a pooled connection
        assert await s.scalar(text("SELECT app_current_org()")) is None
        assert await s.scalar(select(func.count()).select_from(Organization)) == 0


# ---------- audit log ----------


async def test_audit_log_is_append_only(app_sessions: async_sessionmaker[AsyncSession]) -> None:
    org_a, user_a = await create_org_with_owner(app_sessions, f"i-{uuid.uuid4().hex[:8]}")
    async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
        s.add(AuditLog(organization_id=org_a, actor_user_id=user_a, action="org.created"))

    with pytest.raises(ProgrammingError, match="permission denied"):
        async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
            await s.execute(text("UPDATE audit_logs SET action = 'tampered'"))
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with tenant_transaction(app_sessions, org_id=org_a, user_id=user_a) as s:
            await s.execute(text("DELETE FROM audit_logs"))


async def test_audit_trigger_blocks_even_the_owner(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as conn:
        await conn.execute(text("INSERT INTO audit_logs (action) VALUES ('platform.test')"))
        with pytest.raises(DBAPIError, match="append-only"):
            await conn.execute(text("UPDATE audit_logs SET action = 'x'"))
        await conn.rollback()
