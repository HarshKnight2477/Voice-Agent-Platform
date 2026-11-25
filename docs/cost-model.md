# Cost Model, Metering and Billing

## Cost drivers per call minute (illustrative, to be replaced with real measured numbers and current price lists)
| Component | Driver |
|---|---|
| Telephony | carrier per-minute + number rental |
| STT | audio seconds |
| LLM (Anthropic) | input/output tokens per turn (system prompt cached where supported) |
| TTS | characters synthesised |
| LiveKit | self-hosted infra or LiveKit Cloud minutes |
| Storage | recordings, documents, embeddings |

We do not publish per-minute numbers until measured on real calls.

## Metering
Runtime and adapters emit `usage.recorded` events (kind, quantity, provider, model, call_id, org) with idempotency keys → `usage_records`. A metering job aggregates per org/agent/day and computes cost with a versioned `price_book`. Billing is independent of the runtime.

## Guards
Per-org: max concurrent calls, monthly budget with alerts (50/80/100%), max call duration, max tool iterations per turn/call, max workflow runtime, outbound call caps. Over budget: block new outbound calls and new test sessions; **never cut a live inbound call mid-conversation** — degrade gracefully and alert.

## Cost levers
Prompt caching, short spoken responses, smaller model for routing/summaries, deterministic code for rules, retrieval caps, TTS caching of fixed phrases (greetings, fillers).
