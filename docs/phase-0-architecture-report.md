# Phase 0 Architecture Report

**Project:** AI Voice Agent SaaS Platform (LiveKit Agents + LLM provider)
**Date:** 2026-11-25
**Status:** Draft for approval. No application code has been written.

This report answers the master prompt's "First Command" (section 58) and Phase 0 deliverables (section 54). Detailed designs live in the other files in `/docs`; this file is the summary and the index.

---

## 1. What exists today (repository inspection)

| # | Question | Finding |
|---|---|---|
| 1 | What already exists | Nothing. The project folder was empty before this report. Not a git repository. |
| 2 | Existing architecture | None. Greenfield. |
| 3 | Development skills | None in this folder. The 26 skills written earlier lived only in the project's shared files and were written for jambonz. |
| 4 | LiveKit skills | None installed. LiveKit publishes official Agent Skills (`lk skills install`) and a Docs MCP server (`https://docs.livekit.io/mcp`). Neither is installed. |
| 5 | Dependencies installed (PC) | Node 24.20, npm 11.19, Python 3.12.10, git 2.55, GitHub CLI 2.102. **Missing:** Docker, PostgreSQL, Redis, pnpm, uv, LiveKit CLI (`lk`), `livekit-server`. |
| 6 | Environment variables | None defined yet. Expected set is listed in [deployment.md](deployment.md#environment-variables). |
| 7 | Database | None. |
| 8 | APIs | None. |
| 9 | Frontend | None. |
| 10 | Tests | None. |

A separate prototype exists in a separate folder (`AI call bot`) (FastAPI + Anthropic SDK, single restaurant bot). It is not reused as code; its restaurant behaviour becomes the first industry template.

---

## 2. Responsibility split

### WHAT WE BUILD
The SaaS control plane and everything that makes this a business product:
- Tenant system: organizations, memberships, roles, permissions, tenant isolation (Postgres RLS + app-layer checks).
- Authentication, sessions, MFA, API keys.
- Agent model: templates, configurations, immutable versions, publish/rollback lifecycle.
- **Agent runtime** (runs inside the LiveKit agent worker): conversation state, workflow engine, tool gateway, guardrails, prompt assembly, memory, fallback logic.
- Tool framework with typed schemas, explicit permissions, rate limits, idempotency, audit.
- Knowledge system: ingestion, parsing, chunking, embeddings, retrieval, validation.
- Integration framework (calendars, CRM, SMS, email, custom REST, approved MCP servers) with encrypted credentials.
- Telephony domain: phone numbers, carriers, routing rules, business hours, transfers — mapped onto LiveKit SIP by an adapter.
- Usage metering, cost tracking, budgets, billing.
- Dashboard, agent builder, call inspector, live monitor, test/evaluation system.
- Observability conventions, audit logs, retention/redaction.

### WHAT LIVEKIT PROVIDES
- Realtime media transport (WebRTC SFU), rooms, participants, tracks.
- **LiveKit SIP**: inbound/outbound trunks, dispatch rules, SIP participants, cold transfer (SIP REFER via `TransferSIPParticipant`), DTMF, Krisp noise cancellation flag.
- **LiveKit Agents framework** (Python `livekit-agents` ~1.8): `AgentServer`, `@server.rtc_session(agent_name=...)`, jobs/worker processes, `AgentSession`, `Agent`, `@function_tool`, `AgentTask` / `TaskGroup`, handoffs, `MCPToolset`, turn detector (`inference.TurnDetector`), Silero VAD, interruption handling, STT/LLM/TTS plugin pipeline, per-turn metrics, session reports, test helpers and judges, `WarmTransferTask` (beta, Python only).
- Explicit agent dispatch (`CreateAgentDispatch`, `room_config.agents` on dispatch rules) with job metadata (up to 512 KiB).
- **Egress** (`StartEgress` with `TemplateSource`/`MediaSource`, audio-only) for recordings.
- Server APIs (`LiveKitAPI`: `room`, `sip`, `agent_dispatch`) and webhooks.

### WHAT THE LLM PROVIDES
Reasoning and language: caller intent understanding, conversation generation, tool selection (it *requests* tools; it never executes them), structured extraction into conversation state, workflow branch decisions where the workflow allows LLM choice, policy adherence, summaries for handoff and the call inspector. Accessed via the LiveKit Anthropic plugin (`anthropic.LLM`) behind our `LLMProvider` interface.

### WHAT THIRD-PARTY PROVIDERS PROVIDE
- SIP carriers (BYOC): Telnyx, Plivo, Exotel, Twilio Elastic SIP, Vonage, DIDWW — phone numbers and PSTN connectivity.
- STT (Deepgram, AssemblyAI, …), TTS (Cartesia, ElevenLabs, …), embeddings (Voyage/OpenAI/local), optional LiveKit Inference.
- Business systems: Google/Microsoft calendars, HubSpot/Salesforce/Zoho, Shopify, POS/booking systems, SMS/email.
- Infrastructure: Postgres host, object storage (S3-compatible), error tracking, OTel backend.

### WHAT MUST REMAIN UNDER OUR CONTROL
Tenant isolation · security · authorization · business policies · tool permissions · agent configuration · workflow rules · customer data · knowledge · billing · audit logs · agent versions · evaluation · product UX · business logic. None of these may live only inside LiveKit, a carrier, or a model prompt.

---

## 3. Key decisions (proposed — need your approval)

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Agent worker language | **Python** (`livekit-agents` ~1.8) | Python is ahead of Node in the current docs: warm transfer, evals/judges, session-scoped tool mocks, dynamic endpointing, prebuilt tasks, Anthropic `timeout` are Python-only. |
| D2 | Control plane language | **Python 3.12 + FastAPI** | One backend language shared with the worker: one domain model, one set of Pydantic schemas, one tool framework. Modular monolith. |
| D3 | Frontend | **Next.js (App Router) + TypeScript + Tailwind + shadcn/ui**, generated typed API client from OpenAPI | Polished SaaS UI, accessible components, light/dark. |
| D4 | Database | **PostgreSQL 16 + pgvector**, SQLAlchemy 2 + Alembic, **Row-Level Security** on every tenant table | Spec requires Postgres; RLS gives DB-level tenant isolation as defense in depth. pgvector avoids a separate vector DB for MVP. |
| D5 | Queue / cache | **Redis** (also required by LiveKit SIP) + a simple durable job runner (outbox table + worker) | Avoids extra infra; events are written transactionally (outbox) then dispatched. |
| D6 | Process layout | One repo, three deployables from one codebase: `api` (control plane), `worker` (LiveKit agent server), `jobs` (ingestion, metering, webhooks) + `web` | Modular monolith that separates control plane from realtime plane without microservices. |
| D7 | Runtime config delivery | Worker fetches an **immutable, pre-compiled agent version snapshot** (cached, signed) at job start; never queries the dashboard DB on the hot path | Active calls stay pinned to their version; slow dashboard queries cannot block calls. |
| D8 | Local LiveKit | `livekit-server --dev` + `livekit/sip` + Redis via **Docker Compose**; LiveKit Cloud is an optional alternative for SIP testing | SIP needs reachable ports 5060 + RTP 10000-20000; easiest with Docker or LiveKit Cloud. |
| D9 | Monorepo tooling | `uv` workspaces (Python) + `pnpm` (web) | Fast, reproducible. |

**Local blockers:** Docker Desktop, uv, pnpm and the LiveKit CLI are not installed. They are needed from Phase 1 (Postgres/Redis) and Phase 3 (LiveKit). I will ask before installing any of them.

---

## 4. Architecture summary

```
                         ┌──────────────── CONTROL PLANE (FastAPI modular monolith) ───────────────┐
 Browser (Next.js) ──▶   │ auth · orgs/RBAC · agents/versions · templates · knowledge · tools ·      │
                         │ workflows · integrations · telephony domain · calls/analytics · billing   │
                         │            │ outbox events ─▶ jobs runner (ingest, metering, webhooks)    │
                         └────────────┼──────────────────────────────────────────────────────────────┘
                                      │ PostgreSQL (RLS, pgvector) · Redis · object storage
                                      │ (published version snapshots, usage events, call records)
 ┌────────────────────────────── REALTIME / DATA PLANE ─────────────────────────────────────────────┐
 │ Caller ─PSTN─▶ Carrier (BYOC) ─SIP─▶ LiveKit SIP ─▶ Room ─dispatch(agent_name, metadata)─▶       │
 │ LiveKit Agent Server (our worker) ─▶ AgentSession [VAD · turn detector · STT · LLM · TTS]        │
 │      └─ Our Agent Runtime: state · workflow engine · tool gateway · guardrails · memory          │
 │            └─ LLM (anthropic.LLM via LLMProvider) ─ tool requests ─▶ Tool Gateway ─▶ tools    │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Bounded modules (Python packages under `platform/`): `identity`, `tenancy`, `agents`, `runtime`, `tools`, `workflows`, `knowledge`, `telephony`, `voice`, `integrations`, `calls`, `analytics`, `metering`, `audit`, `shared`. Provider adapters live in `platform/providers/<kind>/<vendor>.py`. LiveKit code lives only in `platform/providers/telephony/livekit/` and `platform/voice/livekit_worker/`.

---

## 5. Phase 0 deliverable index

| Deliverable | Document |
|---|---|
| System architecture, module map, ADRs | [architecture.md](architecture.md) |
| LiveKit architecture (SIP, dispatch, deployments, egress) | [livekit.md](livekit.md) |
| LiveKit Agents usage and boundaries | [livekit-agents.md](livekit-agents.md) |
| Voice-agent architecture, latency budget | [voice-architecture.md](voice-architecture.md) |
| LLM architecture, agent runtime, state, memory, tools | [agent-runtime.md](agent-runtime.md) |
| Database design | [database.md](database.md) |
| Security architecture, tool permission model, prompt injection | [security.md](security.md) |
| Multi-tenant model, RBAC | [security.md](security.md#multi-tenancy) and [database.md](database.md#tenant-isolation) |
| RAG architecture | [rag.md](rag.md) |
| Workflow architecture | [workflows.md](workflows.md) |
| API architecture, events | [api.md](api.md) |
| Integrations framework, MCP | [integrations.md](integrations.md) |
| Frontend architecture | [frontend.md](frontend.md) |
| Deployment architecture | [deployment.md](deployment.md) |
| Observability architecture | [observability.md](observability.md) |
| Cost model and billing | [cost-model.md](cost-model.md) |
| Testing strategy and evaluation | [testing.md](testing.md) |
| Troubleshooting | [troubleshooting.md](troubleshooting.md) |
| Disaster recovery | [disaster-recovery.md](disaster-recovery.md) |
| MVP scope, roadmap, risks | sections 6-8 below |

---

## 6. MVP scope

The MVP proves the 18 points of master prompt section 55 for **one industry template (Restaurant Receptionist)** plus a **generic receptionist**, end to end, reliably:

1. Sign up, create organization, invite members (OWNER/ADMIN/VIEWER minimum, full role set modelled).
2. Create agent from template; basic-mode wizard (business, purpose, personality, voice, knowledge, capabilities, rules, handoff, number, test, publish).
3. Knowledge: upload PDF/DOCX/TXT/CSV, FAQ entries, business hours (structured), website single-page fetch.
4. Voice: one STT (Deepgram), one TTS (Cartesia), the LLM via the LiveKit Anthropic plugin; turn detector + Silero VAD; interruptions on.
5. Phone number: one BYOC carrier (Telnyx recommended for first carrier test) via LiveKit SIP inbound trunk + dispatch rule.
6. Inbound calls only (outbound designed, built post-MVP).
7. Tools: restaurant set (`check_availability`, `create_reservation`, `cancel_reservation`, `get_hours`, `get_menu`, `send_confirmation`) against a built-in reservations store, plus `transfer_to_human`.
8. Cold transfer to a human number; handoff summary stored and shown.
9. Recording (audio-only egress) and transcript per tenant setting; retention + delete.
10. Dashboard: calls list, KPIs, call inspector with timeline (STT → LLM → tool → TTS) and latency.
11. Test console: browser call to a **draft** version; scripted text tests with LiveKit test helpers and judges.
12. Versioning: draft → published → rollback; active calls pinned.
13. Usage metering events and per-call cost estimate.

**Explicitly not in MVP:** outbound campaigns, warm transfer, MCP servers, CRM/calendar integrations, visual workflow editor, billing payments, multi-region, live call monitoring UI (data captured, UI later), SSO/OAuth login (email+password+MFA first).

## 7. Post-MVP roadmap

| Order | Item |
|---|---|
| 1 | Outbound calls (`CreateSIPParticipant`), callbacks, calling-hours rules |
| 2 | Warm transfer (`WarmTransferTask`), department routing, after-hours routing, voicemail |
| 3 | Google/Microsoft calendar, HubSpot, SMS/email integrations |
| 4 | Templates: dental, healthcare scheduler, real estate, hotel, automotive, retail, home services |
| 5 | Approved MCP servers per tenant |
| 6 | Visual workflow editor, advanced mode |
| 7 | Live call monitoring UI |
| 8 | Billing provider integration, plans, quotas UI |
| 9 | OAuth/SSO, SCIM |
| 10 | Provider failover (fallback STT/TTS/LLM), multi-LiveKit-deployment routing, regions |
| 11 | Regulated-industry controls (BAA-ready configuration, redaction pipelines) — no compliance claims until verified |

## 8. Technical risks

| Risk | Impact | Mitigation |
|---|---|---|
| End-to-end latency over ~1s feels robotic | High | Streaming everywhere, short responses, measure per-turn `e2e_latency`, `llm_node_ttft`, `tts_node_ttfb`; regional colocation of worker/LiveKit/providers; small model for routing. |
| LiveKit API churn (e.g. session-level metrics events already deprecated) | Medium | Pin `livekit-agents ~=1.8`; all LiveKit calls behind adapters; re-check docs on every upgrade; contract tests. |
| Carrier SIP quirks (REFER support, codecs, auth) | Medium | One carrier certified at a time; carrier capability flags in DB; SIP troubleshooting runbook. |
| Local Windows dev without Docker | Medium | Install Docker Desktop, or use LiveKit Cloud free tier for SIP tests. |
| Prompt injection via callers/documents | High | Instruction hierarchy, retrieved text wrapped as data, tool gateway enforces permissions regardless of model output. |
| Cross-tenant data leak | Critical | RLS + server-derived tenant context + tenant-scoped repositories + isolation test suite in CI. |
| Runaway cost (tool loops, long calls) | Medium | Max tool iterations, max call duration (`max_call_duration` on trunk + runtime timer), per-tenant budgets. |
| Self-hosted SIP needs public UDP ports | Medium | Document networking; cloud staging environment for real calls. |
| Regulated industries (healthcare, finance) | High | Configurable controls only; no HIPAA/PCI/SOC 2 claims until audited. |
| Python-only features may change status | Low | Feature flags; adapter isolation. |

---

## 9. Proposed next steps (each needs your OK)

1. **Recreate the development skills locally** (26 skills, `livekit-engineering` replacing `jambonz-engineering`, others updated from jambonz to LiveKit).
2. **Install the LiveKit Docs MCP server** (`https://docs.livekit.io/mcp`) and the LiveKit CLI + official Agent Skills (`winget install LiveKit.LiveKitCLI`, then `lk skills install`).
3. **Install local tooling:** Docker Desktop, uv, pnpm.
4. **Start Phase 1 — Foundation** (auth, orgs, RBAC, Postgres + migrations, logging, config, test infra, API foundation).
