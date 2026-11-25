# Testing Strategy

| Layer | Tooling | What |
|---|---|---|
| Unit | pytest, pytest-asyncio | domain, workflow engine, tool gateway layers, state extraction, prompt assembly, date/time parsing |
| Integration | pytest + Docker Postgres/Redis (testcontainers) | repositories, RLS, migrations, API endpoints |
| **Tenant isolation** | generated tests over every endpoint with tenant A/B | no cross-tenant read/write; RLS on with app role |
| Security | authz matrix per role, CSRF, webhook signature/replay, SSRF guard, upload fuzzing | |
| Agent behaviour | LiveKit Agents test helpers: `AgentSession.run(user_input=…)`, `result.expect.next_event().is_function_call(...)`, `.judge(llm, intent=...)`, `mock_tools` | per-template scenarios, text mode, fast |
| Evaluation | `livekit.agents.evals` judges (task completion, tool use, accuracy, relevancy) + our deterministic checks | regression gate before publish |
| Voice | browser/SIP test calls with recorded caller audio (noise, fast speech, interruptions, silence) | latency, turn-taking, barge-in, audio quality |
| Load | synthetic concurrent sessions against staging LiveKit | worker scaling, p95 latency |
| Failure | provider fault injection (timeouts, 5xx, rate limits) | fallbacks, safe phrases, escalation |
| Frontend | Vitest + Testing Library, Playwright e2e, axe accessibility | |

## Simulated caller personas (eval datasets)
normal, angry, confused, interrupting, silent, fast-speaking, noisy background, unsupported question, requests human, tool failure, API failure, missing knowledge, no availability, invalid customer data, adversarial input, prompt injection (spoken and via uploaded document).

## Metrics tracked
Task completion, correctness, hallucination rate, tool correctness, policy adherence, escalation correctness, conversation quality, latency, recovery behaviour. Every agent version must pass its regression set before publish (gate configurable per tenant, mandatory for templates).

## Definition of done
Works, secure, tested, observable, handles failure, tenant-isolated, server-side authz, documented, performant; for voice also latency, turn-taking, interruption, audio quality, error recovery, handoff verified.
