"""Talk to an agent by typing, using the real LLM. For local testing only.

  uv run python -m voice_platform.runtime.console --template restaurant_receptionist \
      --business "Joe's Diner" --timezone Asia/Kolkata

Needs ANTHROPIC_API_KEY and LLM_MODEL_* in .env. Type your lines as the caller; an empty
line simulates silence; Ctrl+C or "bye" ends. Messages the agent takes are printed, not saved.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from voice_platform.agents.templates import TEMPLATES
from voice_platform.providers.llm.anthropic_provider import AnthropicLLMProvider
from voice_platform.runtime.session import CallSession, SessionIdentity, TurnAction, TurnResult
from voice_platform.shared.config import get_settings
from voice_platform.shared.logging import configure_logging
from voice_platform.tools.base import ToolContext
from voice_platform.tools.builtin import BUILTIN_TOOLS
from voice_platform.tools.gateway import ToolGateway
from voice_platform.tools.registry import ToolRegistry


class PrintMessageSink:
    async def save(self, ctx: ToolContext, message: dict[str, str]) -> str:
        print(
            f"    [message saved] {message['caller_name']} / {message['callback_number']}: "
            f"{message['message']}"
        )
        return "console"


def show(result: TurnResult, debug: bool) -> None:
    print(f"AGENT: {result.say}")
    if debug:
        m = result.metrics
        tools = [e["name"] + ":" + e["outcome"] for e in result.events if e["type"] == "tool"]
        print(
            f"    [llm calls {m.llm_calls}, {m.llm_latency_ms:.0f} ms; tools {tools}; "
            f"tokens in {m.usage.input_tokens} (cached {m.usage.cache_read_tokens}) "
            f"out {m.usage.output_tokens}]"
        )
    if result.action is TurnAction.TRANSFER and result.handoff:
        print(f"    [TRANSFER to a person] {result.handoff.summary}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", default="generic_receptionist", choices=sorted(TEMPLATES))
    parser.add_argument("--business", default="Demo Business")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--transfer-number", default="")
    parser.add_argument("--debug", action="store_true", help="show timings, tools and tokens")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", json_output=False)
    if not settings.anthropic_api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY (and LLM_MODEL_*) in .env first.")

    config = TEMPLATES[args.template].build_config(args.business, args.timezone)
    if args.transfer_number:
        config = config.model_copy(
            update={
                "escalation": config.escalation.model_copy(
                    update={"transfer_number": args.transfer_number}
                )
            }
        )
    registry = ToolRegistry(BUILTIN_TOOLS)
    session = CallSession(
        SessionIdentity(
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            args.template,
            f"console-{uuid.uuid4().hex[:8]}",
        ),
        config,
        AnthropicLLMProvider(settings),
        registry,
        ToolGateway(registry),
        services={"message_sink": PrintMessageSink()},
    )
    show(session.greeting(), args.debug)
    while not session.ended:
        try:
            line = await asyncio.to_thread(input, "CALLER: ")
        except (EOFError, KeyboardInterrupt):
            break
        if line.strip().lower() in {"bye", "quit", "exit"}:
            break
        result = await (
            session.handle_user_turn(line) if line.strip() else session.handle_silence()
        )
        show(result, args.debug)
        if result.action in (TurnAction.TRANSFER, TurnAction.END_CALL):
            break


if __name__ == "__main__":
    asyncio.run(main())
