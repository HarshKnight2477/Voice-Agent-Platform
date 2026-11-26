"""Creating calls, and loading what a worker needs to run one."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.agents.models import Agent, AgentVersion
from voice_platform.calls.models import Call
from voice_platform.infrastructure.database import tenant_transaction


class CallSetupError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def room_name_for(call_id: uuid.UUID) -> str:
    # Opaque: LiveKit logs room names, so no names or phone numbers here.
    return f"call-{call_id.hex}"


async def create_test_call(
    s: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    version_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> tuple[Call, AgentVersion]:
    agent = await s.scalar(select(Agent).where(Agent.id == agent_id, Agent.status == "active"))
    if agent is None:
        raise CallSetupError("not_found", "Agent not found.", 404)
    stmt = select(AgentVersion).where(AgentVersion.agent_id == agent_id)
    stmt = (
        stmt.where(AgentVersion.id == version_id)
        if version_id
        else stmt.order_by(AgentVersion.number.desc()).limit(1)
    )
    version = await s.scalar(stmt)
    if version is None:
        raise CallSetupError(
            "no_version",
            "Save a version of this agent before testing it."
            if version_id is None
            else "Version not found.",
            409 if version_id is None else 404,
        )
    call_id = uuid.uuid4()
    call = Call(
        id=call_id,
        organization_id=organization_id,
        agent_id=agent_id,
        agent_version_id=version.id,
        direction="test",
        channel="web",
        room_name=room_name_for(call_id),
        created_by_user_id=user_id,
    )
    s.add(call)
    await s.flush()
    return call, version


def dispatch_metadata(call: Call) -> dict[str, Any]:
    return {
        "call_id": str(call.id),
        "organization_id": str(call.organization_id),
        "agent_version_id": str(call.agent_version_id),
    }


@dataclass(frozen=True)
class LoadedCall:
    call_id: uuid.UUID
    organization_id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    agent_name: str
    config: AgentConfig


async def load_call(sm: async_sessionmaker[AsyncSession], metadata: dict[str, Any]) -> LoadedCall:
    """Worker side: resolve dispatch metadata to a call and its pinned agent version.
    Every id is re-checked against the database inside the organization's context, so a
    mismatched or forged combination finds nothing."""
    try:
        call_id = uuid.UUID(str(metadata["call_id"]))
        org_id = uuid.UUID(str(metadata["organization_id"]))
        version_id = uuid.UUID(str(metadata["agent_version_id"]))
    except (KeyError, ValueError) as exc:
        raise CallSetupError("bad_metadata", "dispatch metadata is incomplete") from exc
    async with tenant_transaction(sm, org_id=org_id, user_id=None) as s:
        row = (
            await s.execute(
                select(Call, AgentVersion, Agent)
                .join(AgentVersion, AgentVersion.id == Call.agent_version_id)
                .join(Agent, Agent.id == Call.agent_id)
                .where(
                    Call.id == call_id,
                    Call.agent_version_id == version_id,
                    Call.status == "created",
                )
            )
        ).first()
    if row is None:
        raise CallSetupError("not_found", "call not found or already started", 404)
    call, version, agent = row
    return LoadedCall(
        call_id=call.id,
        organization_id=call.organization_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        agent_name=agent.name,
        config=AgentConfig.model_validate(version.snapshot["config"]),
    )
