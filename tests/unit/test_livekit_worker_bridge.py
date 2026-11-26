"""The bridge between LiveKit's voice pipeline and the platform runtime (no LiveKit server)."""

from __future__ import annotations

import uuid
from typing import Any

from livekit.agents import llm

from voice_platform.agents.config_schema import AgentConfig
from voice_platform.providers.llm.scripted import ScriptedLLMProvider
from voice_platform.runtime.llm import LLMResponse, StopReason, ToolCall
from voice_platform.runtime.session import CallSession, SessionIdentity, TurnAction
from voice_platform.tools.builtin import BUILTIN_TOOLS
from voice_platform.tools.gateway import ToolGateway
from voice_platform.tools.registry import ToolRegistry
from voice_platform.voice.livekit_worker import PlatformAgent


class FakeRecorder:
    def __init__(self) -> None:
        self.turns: list[tuple[str | None, str, int]] = []

    async def record_turn(
        self, caller_text: str | None, result: Any, *, turns: int, intent: str | None
    ) -> None:
        self.turns.append((caller_text, result.say, turns))

    async def record(self, record: Any) -> None:
        return None


def make_agent(steps: list[Any], transfer_number: str = "") -> tuple[PlatformAgent, FakeRecorder]:
    config = AgentConfig.model_validate(
        {
            "business": {"name": "Diner"},
            "tools": [{"name": "transfer_to_human"}],
            "escalation": {"transfer_number": transfer_number},
        }
    )
    registry = ToolRegistry(BUILTIN_TOOLS)
    recorder = FakeRecorder()
    runtime = CallSession(
        SessionIdentity(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "Desk", "c1"),
        config,
        ScriptedLLMProvider(steps),
        registry,
        ToolGateway(registry, recorder),
    )
    runtime.greeting()
    return PlatformAgent(runtime, recorder), recorder  # type: ignore[arg-type]


def ctx_with(*user_lines: str) -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    for line in user_lines:
        ctx.add_message(role="user", content=line)
    return ctx


async def collect(agent: PlatformAgent, ctx: llm.ChatContext) -> list[str]:
    return [chunk async for chunk in agent.llm_node(ctx, [], None)]  # type: ignore[arg-type]


async def test_latest_caller_turn_drives_the_runtime_and_is_recorded() -> None:
    agent, recorder = make_agent([LLMResponse(text="We open at nine.", stop_reason=StopReason.END)])
    out = await collect(agent, ctx_with("earlier line", "When do you open?"))
    assert out == ["We open at nine."]
    assert recorder.turns == [("When do you open?", "We open at nine.", 1)]
    assert agent.pending_action is None


async def test_transfer_decision_is_handed_to_the_worker() -> None:
    agent, _ = make_agent(
        [
            LLMResponse(
                text="",
                stop_reason=StopReason.TOOL_USE,
                tool_calls=(ToolCall("t", "transfer_to_human", {"reason": "asked"}),),
            ),
            LLMResponse(text="Connecting you now.", stop_reason=StopReason.END),
        ],
        transfer_number="+14155550123",
    )
    out = await collect(agent, ctx_with("Put me through to a person"))
    assert out == ["Connecting you now."]
    assert agent.pending_action is not None
    assert agent.pending_action.action is TurnAction.TRANSFER
