"""The LiveKit agent worker: joins call rooms and runs the platform's agent runtime.

Run:  uv run python -m voice_platform.voice.livekit_worker dev

Division of labour (checked against docs.livekit.io and the livekit/agents examples,
livekit-agents 1.9, 2026-11-26):
- LiveKit's AgentSession owns audio: VAD (Silero), speech-to-text, text-to-speech,
  turn-taking and interruptions.
- Our CallSession owns the conversation: prompts, call state, the Tool Gateway, limits,
  escalation. The bridge is `llm_node`: LiveKit calls it with the finished user turn and
  speaks whatever it yields. A placeholder LLM object is passed only to enable the
  pipeline's reply step (the pattern used in livekit/agents' llamaindex example).
- Preemptive generation is OFF: replies may run tools with side effects, so a reply must
  only be produced for a turn the caller has actually finished.
- Explicit dispatch only (agent_name). Job metadata carries ids; the call and its pinned
  agent version are loaded from the database inside the organization's context.
LiveKit types stay in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterable
from typing import Any

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    TurnHandlingOptions,
    cli,
    llm,
)
from livekit.plugins import silero

from voice_platform.calls.recorder import CallRecorder
from voice_platform.calls.service import CallSetupError, load_call
from voice_platform.infrastructure import models_registry  # noqa: F401  (all tables known to the ORM)
from voice_platform.infrastructure.database import create_engine, create_sessionmaker
from voice_platform.providers.llm.factory import build_llm_provider
from voice_platform.runtime.session import CallSession, SessionIdentity, TurnAction, TurnResult
from voice_platform.shared.config import Settings, get_settings
from voice_platform.shared.logging import configure_logging, get_logger
from voice_platform.tools.builtin import BUILTIN_TOOLS
from voice_platform.tools.gateway import ToolGateway
from voice_platform.tools.registry import ToolRegistry
from voice_platform.voice.speech_stack import build_stt, build_tts

log = get_logger(__name__)


class _RuntimeDrivenLLM(llm.LLM):  # type: ignore[type-arg]
    """Never called: PlatformAgent.llm_node produces every reply. LiveKit only needs an LLM
    object present to run the reply step of the voice pipeline."""

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("replies come from PlatformAgent.llm_node")


class PlatformAgent(Agent):
    def __init__(self, runtime: CallSession, recorder: CallRecorder) -> None:
        super().__init__(instructions="")  # instructions live in the platform's prompt layers
        self._runtime = runtime
        self._recorder = recorder
        self._lock = asyncio.Lock()
        self.pending_action: TurnResult | None = None

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[str]:
        del tools, model_settings
        caller_text = ""
        for item in reversed(chat_ctx.items):
            if isinstance(item, llm.ChatMessage) and item.role == "user":
                caller_text = item.text_content or ""
                break
        async with self._lock:  # one turn at a time, in order
            result = await self._runtime.handle_user_turn(caller_text)
        await self._recorder.record_turn(
            caller_text, result, turns=self._runtime.state.turns, intent=self._runtime.state.intent
        )
        if result.action is not TurnAction.CONTINUE:
            self.pending_action = result
        if result.say:
            yield result.say


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server = AgentServer()
server.setup_fnc = prewarm


def _settings() -> Settings:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    return s


@server.rtc_session(agent_name=get_settings().livekit_agent_name)
async def entrypoint(ctx: JobContext) -> None:
    settings = _settings()
    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    try:
        metadata = json.loads(ctx.job.metadata or "{}")
        loaded = await load_call(sm, metadata)
    except (CallSetupError, json.JSONDecodeError) as exc:
        log.warning("worker.call_rejected", reason=str(exc), room=ctx.room.name)
        await engine.dispose()
        ctx.shutdown(reason="call not found")
        return

    recorder = CallRecorder(sm, loaded.organization_id, loaded.call_id)
    registry = ToolRegistry(BUILTIN_TOOLS)
    llm_provider = build_llm_provider(settings)
    runtime = CallSession(
        SessionIdentity(
            loaded.organization_id,
            loaded.agent_id,
            loaded.agent_version_id,
            loaded.agent_name,
            str(loaded.call_id),
        ),
        loaded.config,
        llm_provider,
        registry,
        ToolGateway(registry, recorder),
        services={"message_sink": recorder},
    )
    agent = PlatformAgent(runtime, recorder)
    adv = loaded.config.advanced
    session: AgentSession[Any] = AgentSession(
        stt=build_stt(settings, loaded.config),
        tts=build_tts(settings, loaded.config),
        vad=ctx.proc.userdata["vad"],
        llm=_RuntimeDrivenLLM(),
        turn_handling=TurnHandlingOptions(
            turn_detection="vad",
            endpointing={
                "mode": "fixed",
                "min_delay": adv.endpointing_min_delay_ms / 1000,
                "max_delay": adv.endpointing_max_delay_ms / 1000,
            },
            interruption={
                "enabled": adv.interruptions_enabled,
                # Adaptive (model-based) interruption is a LiveKit Cloud feature; locally use VAD.
                "mode": "vad",
                "min_words": adv.min_interruption_words,
            },
            preemptive_generation={"enabled": False},
        ),
    )

    end_reason = {"value": "caller_hung_up", "status": "completed"}

    async def on_shutdown(*_: Any) -> None:
        try:
            await recorder.end(
                status=end_reason["status"],
                end_reason=end_reason["value"],
                outcome=runtime.state.intent,
            )
        finally:
            close = getattr(llm_provider, "aclose", None)
            if close is not None:
                await close()
            await engine.dispose()

    ctx.add_shutdown_callback(on_shutdown)

    await session.start(room=ctx.room, agent=agent)
    await recorder.mark_started()
    greeting = runtime.greeting()
    await recorder.record_turn(None, greeting, turns=0, intent=None)
    await session.say(greeting.say, allow_interruptions=False, add_to_chat_ctx=False)

    closed = asyncio.Event()

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        # Per-turn latency measured by LiveKit (end of speech, transcription, first audio).
        item = ev.item
        metrics = getattr(item, "metrics", None)
        if metrics:
            log.info(
                "worker.turn_metrics",
                role=getattr(item, "role", None),
                call_id=str(loaded.call_id),
                **{k: round(v, 3) for k, v in dict(metrics).items() if isinstance(v, int | float)},
            )

    @session.on("close")
    def _on_close(_: Any) -> None:  # caller hung up or the room went away
        closed.set()

    # Act on transfer / end-of-call decisions after the reply has been spoken.
    while not closed.is_set():
        try:
            await asyncio.wait_for(closed.wait(), timeout=0.25)
        except TimeoutError:
            pass
        action = agent.pending_action
        if action is None:
            continue
        current = session.current_speech
        if current is not None:
            await current.wait_for_playout()
        if action.action is TurnAction.TRANSFER:
            # Phone transfer arrives with SIP (3.4). Browser calls end with the handoff logged.
            end_reason.update(value="transferred_to_human", status="transferred")
            log.info(
                "worker.transfer_requested",
                call_id=str(loaded.call_id),
                reason=action.handoff.reason if action.handoff else None,
            )
        else:
            end_reason.update(value="agent_ended_call", status="completed")
        break
    ctx.shutdown(reason=end_reason["value"])


def main() -> None:
    # The LiveKit CLI reads its connection settings from the process environment.
    s = get_settings()
    os.environ.setdefault("LIVEKIT_URL", s.livekit_url)
    os.environ.setdefault("LIVEKIT_API_KEY", s.livekit_api_key)
    os.environ.setdefault("LIVEKIT_API_SECRET", s.livekit_api_secret.get_secret_value())
    cli.run_app(server)


if __name__ == "__main__":
    main()
