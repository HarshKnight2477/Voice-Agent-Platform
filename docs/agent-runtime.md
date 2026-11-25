# Agent Runtime and LLM Architecture

## Responsibilities
The runtime is our code that runs inside each LiveKit job. It owns: conversation state, prompt assembly, workflow execution, tool gateway calls, guardrails, memory, fallbacks, usage emission. It is framework-agnostic and unit-testable without LiveKit.

## Agent lifecycle
```
Template ──clone──▶ Agent (tenant-owned) ──edit──▶ AgentConfiguration (draft)
   ──snapshot──▶ AgentVersion vN (immutable, status: testing) ──publish──▶ published (pointer on Agent)
   ──rollback──▶ pointer moves to earlier version
```
A call captures `agent_version_id` at start and keeps it. Publishing changes the pointer only. Publishing requires passing the regression suite (configurable gate).

## Version snapshot (compiled at publish)
JSON document stored on `agent_versions.snapshot`: persona, greeting, prompt layers, voice config, enabled tools with permission grants and schemas, workflow versions, knowledge base ids + retrieval settings, policies (escalation, safety, call limits), business hours, transfer targets. Workers cache by `agent_version_id` (immutable → safe to cache forever).

## Conversation state
```json
{
  "call_id": "…", "intent": "reservation", "detected_intents": ["reservation"],
  "customer": {"name": "John", "phone": "+1…", "verified": false},
  "slots": {"reservation": {"date": "2026-12-05", "party_size": 4, "time": null}},
  "missing": ["reservation.time"],
  "workflow": {"id": "…", "version": 3, "step": "collect_time", "attempts": 0},
  "pending_confirmation": null,
  "auth": {"level": "none"}, "escalation": {"requested": false, "reason": null},
  "actions": [{"tool": "check_availability", "status": "ok", "at": "…"}],
  "counters": {"tool_iterations": 2, "failures": 0}
}
```
Slots are extracted by the LLM via a structured `update_state` tool (schema-validated), and by deterministic parsers (dates/times relative to tenant timezone). State is persisted to Redis per turn for crash recovery and summarised into the call record at the end.

## Prompt assembly (instruction hierarchy)
1. Platform security policy (fixed, not tenant-editable)
2. Tenant security policy
3. Agent policy + persona + industry template guidance
4. Current workflow step instructions + state summary (known / missing slots)
5. Caller input (conversation)
6. Retrieved knowledge and tool outputs — wrapped in `<data source="…">` blocks and labelled untrusted

Small, layered prompts; no giant prompt. Prompt templates are versioned with the agent version.

## LLM usage
| Task | Model tier |
|---|---|
| Live conversation + tool selection | Anthropic Sonnet-class model (confirm the current model id at build time) |
| Intent/route classification, PII detection hints | Haiku-class or deterministic rules |
| Post-call summary, handoff summary, eval judging | Haiku/Sonnet-class, async in `jobs` |
Deterministic code first: business hours, slot validation, availability math, date parsing never go to the model.

## Tool execution path
```
LLM tool request → LiveKit function_tool wrapper (thin) → ToolGateway.execute(ctx, name, args)
  1 schema validation (Pydantic, reject extra fields)
  2 tenant authorization (tool belongs to tenant / is a platform tool enabled for tenant)
  3 agent permission check (explicit grant on this agent version: e.g. READ_CUSTOMER ≠ MODIFY_CUSTOMER)
  4 business rule validation (hours, limits, confirmation obtained for state-changing tools)
  5 rate limit (per tenant/agent/tool/call)
  6 execution (timeout, retry policy only for idempotent tools, idempotency key = call_id+step)
  7 output validation (schema) + sanitisation (strip instructions, size cap)
  8 audit log (tool_executions row)
→ result (or safe ToolError message) → LLM
```
Limits: `max_tool_iterations` per turn (default 4) and per call; loop detection on identical calls.

## Memory
- Short-term: conversation state + recent turns (context window trimmed with summaries for long calls).
- Long-term: `customer_profiles` + `customer_memories` per tenant, written only by explicit, allowed tools/workflow steps under tenant retention policy; sensitive categories require extra permission; deletion on request.

## Human handoff
Triggers: explicit request, repeated failures, tool outage, frustration signal, sensitive topic, policy, uncertainty, workflow step. Before transfer the runtime writes a `handoff` record (customer, reason, summary, slots, attempted actions, tool results) visible in the dashboard and optionally sent to the human via SMS/email/CRM (post-MVP). MVP transfer: cold via `TelephonyProvider.transfer()`.
