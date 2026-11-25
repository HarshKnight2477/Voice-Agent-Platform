# LiveKit Architecture

Verified against docs.livekit.io on 2026-11-25. Re-verify before implementing (Docs MCP: `https://docs.livekit.io/mcp`).

## Components we use
| Component | Role | Self-host notes |
|---|---|---|
| LiveKit Server | SFU, rooms, participants | `livekit-server --dev` → keys `devkey`/`secret`, `127.0.0.1:7880` (`--bind 0.0.0.0` to expose). Windows: GitHub release binary. |
| LiveKit SIP | SIP ↔ room bridge | Separate service, needs Redis. `docker compose` from `livekit/sip` repo. Config: `api_key`, `api_secret`, `ws_url`, `redis.address`, `sip_port` 5060, `rtp_port` 10000-20000. Ports must be reachable from the carrier. |
| LiveKit Egress | Recording | Separate service when self-hosted. `StartEgress` with `TemplateSource` (audio_only) or `MediaSource`. |
| LiveKit Agents | Our worker framework | See [livekit-agents.md](livekit-agents.md). |
| Server API | Admin from control plane | Python `from livekit import api`; `api.LiveKitAPI()` with `.room`, `.sip`, `.agent_dispatch`. |

## Domain ↔ LiveKit mapping (inside `providers/telephony/livekit` only)

| Our domain | LiveKit |
|---|---|
| `Carrier` (BYOC credentials, address) | `SIPInboundTrunkInfo` / `SIPOutboundTrunkInfo` (auth_username/password, address, transport, numbers) |
| `PhoneNumber` → assigned `Agent` | Inbound trunk `numbers` + `SIPDispatchRuleInfo` (`dispatch_rule_individual` with `room_prefix`) with `room_config.agents=[RoomAgentDispatch(agent_name="voice-agent", metadata=...)]` |
| `Call` | One room per call; `call_id` in dispatch metadata and room name |
| Caller / called number | SIP participant attributes `sip.phoneNumber`, `sip.trunkPhoneNumber`; also `sip.callID`, `sip.callStatus`, `sip.trunkID`, `sip.ruleID` |
| `Transfer` (cold) | `TransferSIPParticipant` (SIP REFER; `transfer_to` `tel:+E164` or `sip:`; call must be answered) |
| `Transfer` (warm, post-MVP) | `WarmTransferTask` (beta, Python) or manual: `CreateSIPParticipant(wait_until_answered=True)` + `MoveParticipant` |
| Outbound call (post-MVP) | `CreateSIPParticipant` (`sip_trunk_id`, `sip_call_to`, `room_name`, `wait_until_answered`, `max_call_duration`) after creating an explicit dispatch |
| `Recording` | Egress (audio-only) → our storage |
| DTMF | Room event `sip_dtmf_received`; `publish_dtmf`; `CreateSIPParticipant.dtmf` |
| Max call duration | Trunk `max_call_duration` + runtime timer |
| Noise | `krisp_enabled` on trunk/participant (measure effect) |

## Inbound call flow
1. Carrier sends INVITE to LiveKit SIP for number N.
2. Inbound trunk matches N (auth or allowed addresses required).
3. Dispatch rule creates room `call-<prefix>-…` and dispatches agent `voice-agent` with metadata `{phone_number_id, deployment_id}` (set when the number is assigned).
4. Our worker job starts, resolves `phone_number_id → tenant, agent, published version` via a cached lookup, creates the `Call` record, loads the version snapshot, starts `AgentSession`.
5. Runtime greets, converses, calls tools through the gateway, may transfer.
6. On session end: transcript, session report, usage events, recording reference written; `call.ended` emitted.

Why dispatch-time resolution: a number can be reassigned to another agent without editing LiveKit; publishing a new version changes the snapshot pointer, not LiveKit.

## Multiple LiveKit deployments
`livekit_deployments` table: `id, name, environment (dev|staging|prod), region, url, api_key_ref, api_secret_ref, sip_enabled, egress_enabled`. Each phone number and carrier trunk belongs to a deployment. `TelephonyProvider` is constructed per deployment. The dashboard never talks to LiveKit directly.

## Security
- API keys/secrets stored only server-side (secret manager / encrypted), never in browser.
- Browser test calls get short-lived access tokens minted by our API, scoped to one room, with agent dispatch via token `RoomConfiguration`.
- LiveKit webhooks verified (signed JWT) before use; idempotent by event id.
- Explicit dispatch only (ADR-005).
