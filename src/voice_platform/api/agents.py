"""/api/v1/agent-templates and /api/v1/agents: create, configure, version, publish, roll back."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from voice_platform.agents import service
from voice_platform.agents.config_schema import AgentConfig
from voice_platform.agents.models import Agent, AgentConfiguration, AgentVersion
from voice_platform.agents.templates import TEMPLATES
from voice_platform.api.dependencies import AuthContext, Meta, SessionMaker, require
from voice_platform.api.errors import AppError
from voice_platform.api.pagination import (
    DEFAULT_LIMIT,
    Cursor,
    Limit,
    Page,
    apply_keyset,
    encode_cursor,
)
from voice_platform.audit.models import AuditLog
from voice_platform.identity.service import RequestMeta
from voice_platform.infrastructure.database import tenant_transaction
from voice_platform.tenancy.models import Organization
from voice_platform.tenancy.permissions import Permission

router = APIRouter(prefix="/api/v1", tags=["agents"])
_gate = service.SchemaPublishGate()


def _err(exc: service.AgentError) -> AppError:
    return AppError(exc.status, exc.code, exc.message)


def _audit(
    s: AsyncSession,
    ctx: AuthContext,
    meta: RequestMeta,
    action: str,
    target: uuid.UUID,
    details: dict[str, Any] | None = None,
) -> None:
    s.add(
        AuditLog(
            organization_id=ctx.organization_id,
            actor_user_id=ctx.user_id,
            action=action,
            target_type="agent",
            target_id=str(target),
            ip_address=meta.ip,
            request_id=meta.request_id,
            details=details or {},
        )
    )


# ---------------------------------------------------------------- schemas


class TemplateOut(BaseModel):
    key: str
    name: str
    industry: str
    description: str


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    template_key: str = Field(default="generic_receptionist", max_length=64)


class AgentRename(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    template_key: str | None
    published_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DraftOut(BaseModel):
    agent_id: uuid.UUID
    revision: int
    config: AgentConfig
    updated_at: datetime


class DraftSave(BaseModel):
    revision: int = Field(ge=1)
    config: AgentConfig


class VersionCreate(BaseModel):
    change_note: str = Field(default="", max_length=500)


class VersionSummary(BaseModel):
    id: uuid.UUID
    number: int
    status: str
    change_note: str
    snapshot_hash: str
    created_at: datetime
    published_at: datetime | None


class VersionOut(VersionSummary):
    snapshot: dict[str, Any]


class PublishResult(BaseModel):
    agent_id: uuid.UUID
    published_version: VersionSummary
    previous_version_id: uuid.UUID | None


class RollbackRequest(BaseModel):
    version_id: uuid.UUID


def _agent_out(a: Agent) -> AgentOut:
    return AgentOut(
        id=a.id,
        name=a.name,
        template_key=a.template_key,
        published_version_id=a.published_version_id,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


def _summary(v: AgentVersion) -> VersionSummary:
    return VersionSummary(
        id=v.id,
        number=v.number,
        status=v.status,
        change_note=v.change_note,
        snapshot_hash=v.snapshot_hash,
        created_at=v.created_at,
        published_at=v.published_at,
    )


def _draft_out(d: AgentConfiguration) -> DraftOut:
    return DraftOut(
        agent_id=d.agent_id,
        revision=d.revision,
        config=AgentConfig.model_validate(d.config),
        updated_at=d.updated_at,
    )


# ---------------------------------------------------------------- templates and agents


@router.get("/agent-templates")
async def list_templates(_: AuthContext = require(Permission.AGENTS_READ)) -> list[TemplateOut]:
    return [
        TemplateOut(key=t.key, name=t.name, industry=t.industry, description=t.description)
        for t in TEMPLATES.values()
    ]


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: AgentCreate,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> AgentOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        org = await s.get(Organization, ctx.organization_id)
        assert org is not None
        try:
            agent, _ = await service.create_agent(
                s,
                org_id=ctx.organization_id,
                user_id=ctx.user_id,
                name=body.name,
                template_key=body.template_key,
                business_name=org.name,
                timezone=org.timezone,
            )
        except service.AgentError as exc:
            raise _err(exc) from None
        _audit(s, ctx, meta, "agent.created", agent.id, {"template": body.template_key})
        return _agent_out(agent)


@router.get("/agents")
async def list_agents(
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.AGENTS_READ),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[AgentOut]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        stmt = select(Agent).where(Agent.status == "active")
        rows = list(await s.scalars(apply_keyset(stmt, Agent.created_at, Agent.id, cursor, limit)))
    page = rows[:limit]
    return Page(
        data=[_agent_out(a) for a in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
    )


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID, sm: SessionMaker, ctx: AuthContext = require(Permission.AGENTS_READ)
) -> AgentOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            return _agent_out(await service.get_agent(s, agent_id))
        except service.AgentError as exc:
            raise _err(exc) from None


@router.patch("/agents/{agent_id}")
async def rename_agent(
    agent_id: uuid.UUID,
    body: AgentRename,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> AgentOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            agent = await service.get_agent(s, agent_id, lock=True)
        except service.AgentError as exc:
            raise _err(exc) from None
        agent.name = body.name
        _audit(s, ctx, meta, "agent.renamed", agent.id)
        await s.flush()
        await s.refresh(agent)
        return _agent_out(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_agent(
    agent_id: uuid.UUID,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> None:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            await service.archive_agent(s, agent_id)
        except service.AgentError as exc:
            raise _err(exc) from None
        _audit(s, ctx, meta, "agent.archived", agent_id)


# ---------------------------------------------------------------- draft configuration


@router.get("/agents/{agent_id}/configuration")
async def get_draft(
    agent_id: uuid.UUID, sm: SessionMaker, ctx: AuthContext = require(Permission.AGENTS_READ)
) -> DraftOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            await service.get_agent(s, agent_id)
        except service.AgentError as exc:
            raise _err(exc) from None
        draft = await s.get(AgentConfiguration, agent_id)
        assert draft is not None
        return _draft_out(draft)


@router.put("/agents/{agent_id}/configuration")
async def save_draft(
    agent_id: uuid.UUID,
    body: DraftSave,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> DraftOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            draft = await service.save_draft(
                s,
                agent_id=agent_id,
                user_id=ctx.user_id,
                config=body.config,
                expected_revision=body.revision,
            )
        except service.AgentError as exc:
            raise _err(exc) from None
        _audit(s, ctx, meta, "agent.draft_saved", agent_id, {"revision": draft.revision})
        return _draft_out(draft)


# ---------------------------------------------------------------- versions


@router.post("/agents/{agent_id}/versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    agent_id: uuid.UUID,
    body: VersionCreate,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_WRITE),
) -> VersionSummary:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            v = await service.create_version(
                s, agent_id=agent_id, user_id=ctx.user_id, change_note=body.change_note
            )
        except service.AgentError as exc:
            raise _err(exc) from None
        _audit(s, ctx, meta, "agent.version_created", agent_id, {"number": v.number})
        return _summary(v)


@router.get("/agents/{agent_id}/versions")
async def list_versions(
    agent_id: uuid.UUID,
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.AGENTS_READ),
    limit: Limit = DEFAULT_LIMIT,
    cursor: Cursor = None,
) -> Page[VersionSummary]:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            await service.get_agent(s, agent_id)
        except service.AgentError as exc:
            raise _err(exc) from None
        stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
        rows = list(
            await s.scalars(
                apply_keyset(stmt, AgentVersion.created_at, AgentVersion.id, cursor, limit)
            )
        )
    page = rows[:limit]
    return Page(
        data=[_summary(v) for v in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None,
    )


@router.get("/agents/{agent_id}/versions/{version_id}")
async def get_version(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    sm: SessionMaker,
    ctx: AuthContext = require(Permission.AGENTS_READ),
) -> VersionOut:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        v = await s.scalar(
            select(AgentVersion).where(
                AgentVersion.id == version_id, AgentVersion.agent_id == agent_id
            )
        )
    if v is None:
        raise AppError(404, "not_found", "Version not found.")
    return VersionOut(**_summary(v).model_dump(), snapshot=v.snapshot)


async def _publish(
    sm: SessionMaker,
    ctx: AuthContext,
    meta: RequestMeta,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    action: str,
) -> PublishResult:
    async with tenant_transaction(sm, org_id=ctx.organization_id, user_id=ctx.user_id) as s:
        try:
            agent, version, previous = await service.publish_version(
                s, agent_id=agent_id, version_id=version_id, gate=_gate, trace_id=meta.request_id
            )
        except service.AgentError as exc:
            raise _err(exc) from None
        _audit(
            s,
            ctx,
            meta,
            action,
            agent_id,
            {"version": version.number, "previous": previous.number if previous else None},
        )
        await s.refresh(version)
        return PublishResult(
            agent_id=agent.id,
            published_version=_summary(version),
            previous_version_id=previous.id if previous else None,
        )


@router.post("/agents/{agent_id}/versions/{version_id}/publish")
async def publish(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_PUBLISH),
) -> PublishResult:
    return await _publish(sm, ctx, meta, agent_id, version_id, "agent.published")


@router.post("/agents/{agent_id}/rollback")
async def rollback(
    agent_id: uuid.UUID,
    body: RollbackRequest,
    sm: SessionMaker,
    meta: Meta,
    ctx: AuthContext = require(Permission.AGENTS_PUBLISH),
) -> PublishResult:
    return await _publish(sm, ctx, meta, agent_id, body.version_id, "agent.rolled_back")
