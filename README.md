# Voice-Agent-Platform
This project is a SaaS platform for creating AI phone employees for businesses.  Businesses can choose an industry template—such as restaurant, hotel, insurance, healthcare, real estate, or automotive—and configure an AI agent for their specific business.

## Overview

Multi-tenant SaaS for creating, testing, deploying and monitoring AI phone agents for small and medium businesses. Built on LiveKit Agents + LiveKit SIP, with an LLM provider (Anthropic) as the reasoning model and PostgreSQL as the database.

**Status:** Phase 1 (foundation) in progress: project setup, local services, health checks and the core database with tenant isolation are done. See [docs/build-log.md](docs/build-log.md).

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
