# System Architecture

## Principles
1. **One engine, many industries.** Industry behaviour = template + knowledge + workflows + tools + policies + config. No industry-specific runtime code.
2. **Modular monolith.** One Python codebase, strict module boundaries, three process types. Extract services only when scale demands it.
3. **Control plane vs realtime plane.** Calls never wait on dashboard queries. The worker runs from an immutable version snapshot.
4. **Our domain, their adapters.** The domain speaks `Call`, `Agent`, `Conversation`, `Participant`, `PhoneNumber`, `Transfer`, `Recording`, `Transcript`. LiveKit's `Room`, `SIPParticipant`, `AgentSession`, `JobContext` appear only in adapter packages.
5. **The model requests; the platform decides.** The LLM proposes tool calls; the tool gateway validates, authorizes and executes.
6. **Tenant isolation by design.** Server-derived tenant context + Postgres RLS + tenant-scoped repositories.

## Layering

```
Business → Agent Template → Agent Configuration → Agent Version (immutable)
        → Agent Runtime (state · workflows · tools · knowledge · policies)
        → LLM (LLMProvider) → LiveKit Agents (AgentSession) → LiveKit SIP → Carrier → Customer
```

## Repository layout (planned, created in Phase 1)

```
/
├── platform/                 # Python package (uv workspace member)
│   ├── shared/               # config, errors, ids, clock, tenant context, logging
│   ├── identity/             # users, sessions, MFA, API keys
│   ├── tenancy/              # organizations, memberships, roles, permissions
│   ├── agents/               # templates, agents, configurations, versions, publishing
│   ├── runtime/              # conversation state, prompt assembly, guardrails, memory, orchestrator
│   ├── tools/                # tool registry, gateway (validation → authz → rules → rate limit → exec → audit)
│   ├── workflows/            # workflow definitions, versions, deterministic engine
│   ├── knowledge/            # ingestion, parsing, chunking, embeddings, retrieval
│   ├── telephony/            # phone numbers, carriers, routing rules, business hours (domain only)
│   ├── voice/                # voice config domain + livekit_worker/ (AgentServer entrypoint)
│   ├── integrations/         # integration framework, credential vault, connectors, MCP gateway
│   ├── calls/                # calls, conversations, messages, transcripts, recordings
│   ├── analytics/            # read models, KPIs
│   ├── metering/             # usage events, aggregation, budgets
│   ├── audit/                # audit log
│   ├── events/               # domain events, outbox, dispatcher
│   ├── providers/            # adapters: llm/ stt/ tts/ vad/ embedding/ telephony/livekit/ storage/
│   └── api/                  # FastAPI app, routers /api/v1/*, dependencies, error mapping
├── apps/
│   ├── api/                  # entry: uvicorn platform.api.main:app
│   ├── worker/               # entry: LiveKit AgentServer (platform.voice.livekit_worker)
│   ├── jobs/                 # entry: outbox dispatcher + background jobs
│   └── web/                  # Next.js dashboard
├── migrations/               # Alembic
├── tests/                    # unit / integration / isolation / voice / eval
├── infra/                    # docker-compose, livekit & sip config, IaC later
├── docs/
```

Module rules: modules talk through their public `service.py` interfaces and domain events, never through each other's tables. Each module owns its tables.

## Provider interfaces

| Interface | Primary (MVP) | Notes |
|---|---|---|
| `LLMProvider` | Anthropic via LiveKit `anthropic.LLM` | Also a non-realtime client for summaries/evals. |
| `STTProvider` | Deepgram plugin | AssemblyAI alt. |
| `TTSProvider` | Cartesia plugin | ElevenLabs alt. |
| `VADProvider` | Silero (`silero.VAD.load()`) | |
| `TurnDetectionProvider` | LiveKit `inference.TurnDetector()` | Treated as part of VoiceModelProvider config. |
| `VoiceModelProvider` | Composes STT+LLM+TTS+VAD+turn config into an `AgentSession` | |
| `RealtimeModelProvider` | Not used in MVP | Speech-to-speech models later. |
| `EmbeddingProvider` | Voyage or OpenAI embeddings (decide Phase 4) | |
| `TelephonyProvider` | `LiveKitProvider` | Trunks, dispatch rules, transfer, outbound dial, recording. |
| `StorageProvider` | Local disk (dev) / S3-compatible | Recordings, documents. |

Each adapter maps vendor errors into platform errors and emits latency/usage metrics in a common shape.

## Process types

| Process | Plane | Responsibilities |
|---|---|---|
| `api` | Control | REST API, auth, CRUD, publishing, LiveKit admin calls through `TelephonyProvider`, webhook intake. |
| `worker` | Realtime | LiveKit `AgentServer`; one job subprocess per call; runs our runtime. Talks to the control plane only via a narrow internal API (snapshot fetch, tool execution side effects via DB, event emission). |
| `jobs` | Control | Outbox dispatcher, document ingestion, embeddings, metering aggregation, retention purge, recording post-processing. |
| `web` | Control | Next.js UI. Talks only to `api`. |

## Architecture decision records (summary)
- **ADR-001** Python for worker and control plane (Python Agents SDK has the fuller feature set).
- **ADR-002** PostgreSQL + RLS + pgvector; no separate vector DB in MVP.
- **ADR-003** Outbox pattern for domain events; Redis for queues/cache/rate limits.
- **ADR-004** Immutable agent version snapshots delivered to workers; calls pinned to version.
- **ADR-005** Explicit agent dispatch (`agent_name`) only; never auto-dispatch to every room.
- **ADR-006** LiveKit isolated behind `TelephonyProvider` and the worker adapter.
- **ADR-007** Next.js + typed OpenAPI client for the dashboard.
