# Workflow Architecture

Deterministic, versioned workflows drive multi-step tasks; the LLM handles language and slot extraction inside each step.

## Definition (JSON, versioned, immutable once published)
```json
{
  "id": "restaurant.reservation", "version": 1,
  "variables": {"date": "date", "time": "time", "party_size": "int", "name": "string"},
  "start": "collect",
  "steps": {
    "collect":  {"type": "collect", "slots": ["date", "party_size", "time", "name"], "max_attempts": 3, "next": "check"},
    "check":    {"type": "tool", "tool": "check_availability", "args": {"date": "$date", "time": "$time", "party_size": "$party_size"},
                 "on": {"available": "confirm", "unavailable": "offer", "error": "fallback"}},
    "offer":    {"type": "collect", "prompt_hint": "offer alternatives from result", "slots": ["time"], "next": "check"},
    "confirm":  {"type": "confirm", "summary": "Table for {party_size} on {date} at {time} under {name}", "yes": "book", "no": "collect"},
    "book":     {"type": "tool", "tool": "create_reservation", "idempotent": true, "on": {"ok": "notify", "error": "fallback"}},
    "notify":   {"type": "tool", "tool": "send_confirmation", "optional": true, "next": "end"},
    "fallback": {"type": "escalate", "reason": "tool_failure"},
    "end":      {"type": "end"}
  }
}
```
Step types: `collect`, `validate`, `condition`/`branch`, `tool`, `confirm`, `say`, `escalate`, `business_hours`, `wait`/timeout, `end`. Each step has retries, timeouts and a fallback edge.

## Execution
- The engine (pure Python, in `platform/workflows`) advances on state changes and tool results; persisted in `workflow_executions`.
- Mapped onto LiveKit `AgentTask` where a step needs its own focused instructions/tools; the engine remains the source of truth.
- The LLM can pick *which* workflow to start (intent) and fill slots; it cannot skip confirm or tool steps.
- Business hours and validation are deterministic code.
- Visual editor (post-MVP) edits the same JSON.
