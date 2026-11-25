"""Agent lifecycle: create from template, edit draft, snapshot a version, publish, roll back.

All functions run inside a caller-provided tenant transaction, so row-level security scopes
every query to the caller's organization.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.agents.models import Agent, AgentConfiguration, AgentVersion
from voice_platform.agents.templates import get_template
from voice_platform.events.models import OutboxEvent

SNAPSHOT_SCHEMA_VERSION = 1


class AgentError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def compile_snapshot(agent: Agent, config: AgentConfig, number: int) -> dict[str, Any]:
    """Everything a worker needs to run a call with this version, and nothing else.
    No secrets: provider credentials stay server-side and are resolved at runtime."""
    return {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "organization_id": str(agent.organization_id),
        "agent_id": str(agent.id),
        "agent_name": agent.name,
        "version_number": number,
        "template_key": agent.template_key,
        "config": config.model_dump(mode="json"),
    }


class PublishGate(Protocol):
    """Decides whether a version may go live. Phase 5/7 adds the regression-test gate."""

    async def check(self, version: AgentVersion) -> list[str]:
        """Return reasons it cannot be published (empty list = OK)."""


class SchemaPublishGate:
    async def check(self, version: AgentVersion) -> list[str]:
        try:
            AgentConfig.model_validate(version.snapshot.get("config", {}))
        except ValidationError as exc:
            return [f"configuration is invalid: {exc.error_count()} error(s)"]
        return []


# ---------------------------------------------------------------- agents and drafts


async def create_agent(
    s: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    name: str,
    template_key: str,
    business_name: str,
    timezone: str,
) -> tuple[Agent, AgentConfiguration]:
    template = get_template(template_key)
    if template is None:
        raise AgentError("unknown_template", "That template doesn't exist.", 422)
    config = template.build_config(business_name, timezone)
    agent = Agent(
        id=uuid.uuid4(),
        organization_id=org_id,
        name=name,
        template_key=template_key,
        created_by_user_id=user_id,
    )
    s.add(agent)
    await s.flush()
    draft = AgentConfiguration(
        agent_id=agent.id,
        organization_id=org_id,
        config=config.model_dump(mode="json"),
        revision=1,
        updated_by_user_id=user_id,
    )
    s.add(draft)
    await s.flush()
    await s.refresh(agent)
    return agent, draft


async def get_agent(s: AsyncSession, agent_id: uuid.UUID, *, lock: bool = False) -> Agent:
    stmt = select(Agent).where(Agent.id == agent_id, Agent.status == "active")
    if lock:
        stmt = stmt.with_for_update()
    agent = await s.scalar(stmt)
    if agent is None:  # also the answer for another organization's agent (RLS hides it)
        raise AgentError("not_found", "Agent not found.", 404)
    return agent


async def save_draft(
    s: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    config: AgentConfig,
    expected_revision: int,
) -> AgentConfiguration:
    """Optimistic concurrency: two people editing at once can't silently overwrite each other."""
    await get_agent(s, agent_id)
    draft = await s.scalar(
        select(AgentConfiguration).where(AgentConfiguration.agent_id == agent_id).with_for_update()
    )
    if draft is None:
        raise AgentError("not_found", "Agent not found.", 404)
    if draft.revision != expected_revision:
        raise AgentError(
            "revision_conflict",
            "Someone else saved changes first. Reload to see them, then try again.",
            409,
        )
    draft.config = config.model_dump(mode="json")
    draft.revision += 1
    draft.updated_by_user_id = user_id
    await s.flush()
    await s.refresh(draft)
    return draft


# ---------------------------------------------------------------- versions


async def create_version(
    s: AsyncSession, *, agent_id: uuid.UUID, user_id: uuid.UUID, change_note: str
) -> AgentVersion:
    agent = await get_agent(s, agent_id, lock=True)  # serializes version numbering
    draft = await s.scalar(
        select(AgentConfiguration).where(AgentConfiguration.agent_id == agent_id)
    )
    assert draft is not None
    config = AgentConfig.model_validate(draft.config)  # re-validate: rules may have tightened
    number = (
        await s.scalar(
            select(func.coalesce(func.max(AgentVersion.number), 0)).where(
                AgentVersion.agent_id == agent_id
            )
        )
        or 0
    ) + 1
    snapshot = compile_snapshot(agent, config, number)
    version = AgentVersion(
        organization_id=agent.organization_id,
        agent_id=agent_id,
        number=number,
        snapshot=snapshot,
        snapshot_hash=canonical_hash(snapshot),
        change_note=change_note,
        created_by_user_id=user_id,
    )
    s.add(version)
    await s.flush()
    await s.refresh(version)
    return version


async def publish_version(
    s: AsyncSession,
    *,
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    gate: PublishGate,
    trace_id: str | None,
) -> tuple[Agent, AgentVersion, AgentVersion | None]:
    """Point the agent at `version_id`. Also used for rollback (publishing an older version).
    Calls already in progress keep the version they started with; only new calls change."""
    agent = await get_agent(s, agent_id, lock=True)
    version = await s.scalar(
        select(AgentVersion).where(AgentVersion.id == version_id, AgentVersion.agent_id == agent_id)
    )
    if version is None:
        raise AgentError("not_found", "Version not found.", 404)
    if agent.published_version_id == version.id:
        raise AgentError("already_published", "That version is already live.", 409)
    problems = await gate.check(version)
    if problems:
        raise AgentError("publish_blocked", "; ".join(problems), 422)

    previous: AgentVersion | None = None
    if agent.published_version_id is not None:
        previous = await s.get(AgentVersion, agent.published_version_id)
        await s.execute(
            update(AgentVersion)
            .where(AgentVersion.id == agent.published_version_id)
            .values(status="retired")
        )
    version.status = "published"
    version.published_at = _now()
    agent.published_version_id = version.id
    s.add(
        OutboxEvent(
            organization_id=agent.organization_id,
            event_type="agent.published",
            payload={
                "agent_id": str(agent.id),
                "version_id": str(version.id),
                "version_number": version.number,
                "previous_version_id": str(previous.id) if previous else None,
            },
            trace_id=trace_id,
        )
    )
    await s.flush()
    return agent, version, previous


async def archive_agent(s: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await get_agent(s, agent_id, lock=True)
    agent.status = "archived"
    return agent
