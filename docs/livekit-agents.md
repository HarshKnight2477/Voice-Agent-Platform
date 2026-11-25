# LiveKit Agents Usage

SDK: Python `livekit-agents` (pin `~=1.8`, extras `anthropic`, `deepgram`, `cartesia`, `silero`, `mcp`). Verified 2026-11-25 against docs.livekit.io. **Re-check every API below before coding.**

## Worker entrypoint shape (from current docs)
```python
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent, TurnHandlingOptions, inference

server = AgentServer()

@server.rtc_session(agent_name="voice-agent", on_session_end=on_session_end)
async def entrypoint(ctx: agents.JobContext):
    ...  # our adapter: resolve call → load version snapshot → build session → start

if __name__ == "__main__":
    agents.cli.run_app(server)
```
This is reference only; it lives in `platform/voice/livekit_worker/` and nowhere else.

## What we use natively (do not rebuild)
| Need | LiveKit feature |
|---|---|
| Jobs / scaling | `AgentServer` registers with LiveKit; one subprocess per job; horizontal scale by adding workers |
| Pipeline | `AgentSession(stt=…, llm=…, tts=…, vad=…, turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()))` |
| Interruptions | `turn_handling` interruption options (`enabled`, `mode` adaptive/vad, `min_duration`, `min_words`, `false_interruption_timeout`, `resume_false_interruption`); per-utterance `allow_interruptions=False` for legally required disclosures |
| Tools | `@function_tool` / `raw_schema` — **we register thin wrappers that forward to our Tool Gateway**; `ToolError` returns a safe message to the model |
| Multi-step | `AgentTask[T]`, `TaskGroup` (experimental), handoffs (tool returns another `Agent`) — used as the execution vehicle for our workflow steps |
| MCP | `mcp.MCPToolset` — **not** passed to the model directly; MCP tools are proxied through our gateway (see integrations.md) |
| Transcripts | `conversation_item_added` events, `session.history` |
| Metrics | per-turn `ChatMessage.metrics` (`e2e_latency`, `llm_node_ttft`, `tts_node_ttfb`), `session_usage_updated`, `session.usage.model_usage`; per-plugin `metrics_collected`. **Session-level `metrics_collected` and `UsageCollector` are deprecated — do not use.** |
| Session report | `ctx.make_session_report()` in `on_session_end` |
| Tracing | OpenTelemetry export of session traces (exporter setup to verify in Phase 3) |
| DTMF / IVR | `sip_dtmf_received`, `GetDtmfTask`, `ivr_detection=True` |
| Warm transfer | `WarmTransferTask` (beta) post-MVP |
| Testing | `AgentSession.run(user_input=…)`, `result.expect…`, `mock_tools`, `livekit.agents.evals` judges |

## Adapter boundary
```
LiveKit JobContext ──▶ LiveKitCallAdapter ──▶ CallContext (ours: call_id, tenant_id, agent_version_id, caller, called)
AgentSession events ──▶ adapter ──▶ RuntimeEvents (TurnStarted, UserTranscript, AgentReply, ToolRequested, Metrics)
Runtime decisions  ──▶ adapter ──▶ session.say / generate_reply / update tools / transfer via TelephonyProvider
```
The runtime (`platform/runtime`) is testable without LiveKit.

## Dispatch
- `agent_name="voice-agent"` on the worker → explicit dispatch only.
- Inbound: SIP dispatch rule `room_config.agents`.
- Browser test: token `RoomConfiguration.agents` with metadata `{agent_version_id, mode: "test"}`.
- Outbound: `agent_dispatch.create_dispatch(CreateAgentDispatchRequest(agent_name, room, metadata))` then `CreateSIPParticipant`.
- Metadata: JSON ≤ 512 KiB; we pass only ids, never config or secrets.

## Coding-agent tooling
- Docs MCP server: `https://docs.livekit.io/mcp`
- CLI: `winget install LiveKit.LiveKitCLI`; skills: `lk skills install`
- Local debug: `lk agent dev`, `lk agent debugger …` (verify availability for self-hosted).
