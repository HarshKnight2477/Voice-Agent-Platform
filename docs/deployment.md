# Deployment Architecture

## Environments
local → staging → production. Each environment has its own Postgres, Redis, storage bucket, secrets, and one or more `livekit_deployments` (dev LiveKit, staging LiveKit, production cluster; regional later).

## Local (Windows PC)
Prerequisites to install (ask first): Docker Desktop, uv, pnpm, LiveKit CLI (`winget install LiveKit.LiveKitCLI`).
`infra/docker-compose.yml`: postgres (pgvector image), redis, livekit-server (`--dev`), livekit-sip, (egress later). `api`, `worker`, `jobs` run with `uv run`, `web` with `pnpm dev`.
Real phone calls locally need SIP ports 5060/UDP+TCP and RTP 10000-20000 reachable from the carrier; otherwise use LiveKit Cloud (free tier) or a staging VM for carrier tests. Browser test calls work fully locally.

## Production (target)
- Containers per process type (`api`, `worker`, `jobs`, `web`) from one image family.
- Kubernetes (or a simpler managed container platform first); worker pool autoscaled on active jobs; graceful drain on deploy so live calls finish.
- Self-hosted LiveKit (server + SIP + egress, Redis) or LiveKit Cloud per deployment record.
- Managed Postgres with PITR backups; Redis HA; S3-compatible storage.
- CI/CD: lint, type check, tests (unit, integration, isolation), security scans, build images, migrate, deploy staging, smoke + voice tests, manual promote to prod.
- IaC (Terraform) once the hosting target is chosen.

## Environment variables
| Var | Used by |
|---|---|
| `DATABASE_URL`, `REDIS_URL` | api, worker, jobs |
| `APP_SECRET_KEY`, `CREDENTIALS_MASTER_KEY` | api, jobs, worker |
| `ANTHROPIC_API_KEY` | worker, jobs |
| `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY` (and others per provider) | worker |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (default deployment; others in DB as secret refs) | api, worker |
| `EMBEDDING_PROVIDER`, `EMBEDDING_API_KEY` | jobs, worker |
| `STORAGE_BACKEND`, `S3_*` | api, jobs |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `SENTRY_DSN` | all |
| `INTERNAL_API_TOKEN` | worker → api |
All in `.env` (git-ignored); `.env.example` committed with placeholders only.
