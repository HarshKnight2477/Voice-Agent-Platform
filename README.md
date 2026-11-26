# Voice-Agent-Platform
This project is a SaaS platform for creating AI phone employees for businesses.  Businesses can choose an industry template—such as restaurant, hotel, insurance, healthcare, real estate, or automotive—and configure an AI agent for their specific business.

## Overview

Multi-tenant SaaS for creating, testing, deploying and monitoring AI phone agents for small and medium businesses. Built on LiveKit Agents + LiveKit SIP, with an LLM provider (Anthropic) as the reasoning model and PostgreSQL as the database.

**Status:** Phases 1 (foundation) and 2 (agent runtime) complete; Phase 3 (voice) in progress, local voice calls working. See [docs/build-log.md](docs/build-log.md).

- Architecture and design: [docs/](docs/README.md), starting with [docs/phase-0-architecture-report.md](docs/phase-0-architecture-report.md)

## Local development (Windows, PowerShell)

Prerequisites: Docker Desktop, [uv](https://docs.astral.sh/uv/), Python 3.12.

```powershell
Copy-Item .env.example .env        # then replace every change-me value
docker compose up -d --wait        # PostgreSQL (pgvector) + Redis on 127.0.0.1
uv sync                            # install dependencies into .venv
uv run python -m voice_platform.cli db-setup   # migrate + create the restricted app role
uv run uvicorn voice_platform.api.main:create_app --factory --reload
```

Checks: `http://127.0.0.1:8000/health/live` and `http://127.0.0.1:8000/health/ready`.

```powershell
uv run ruff check .; uv run mypy; uv run pytest -m "not integration"   # fast checks
uv run pytest -m integration                                          # needs docker compose up
```

## Talk to an agent locally (free, no API keys)

One-time: install [Ollama](https://ollama.com) and run `ollama pull qwen3:8b`.

```powershell
docker compose --profile speech up -d --wait                              # DB, Redis, LiveKit, Whisper, Kokoro
uv run uvicorn voice_platform.api.main:create_app --factory                # terminal 1: API
uv run python -m voice_platform.voice.livekit_worker dev                   # terminal 2: voice agent worker
uv run python scripts/browser_test_call.py --business "Joe's Diner"       # terminal 3: opens the call page
```

Press **Start call**, allow the microphone, and talk. The transcript prints in terminal 3 when you hang up.
`uv run python scripts/e2e_voice_call.py` runs the same call automatically with a simulated caller.
