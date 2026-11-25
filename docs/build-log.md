# Build Log

One entry per build step: what was built, why, and how it was verified.

## Phase 1.1 — Project setup and local services
- Python 3.12 project managed with uv; package `src/voice_platform` (`platform` would shadow the Python stdlib).
- Typed settings from `.env` (`shared/config.py`); secrets are `SecretStr`; weak `APP_SECRET_KEY` rejected outside local/test.
- Structured JSON logging with a request ID on every line; password/token/key fields masked (`shared/logging.py`, `shared/request_context.py`).
- `/health/live` and `/health/ready` (PostgreSQL + Redis, 2 s timeout, 503 without internal details).
- `compose.yaml`: PostgreSQL 16 + pgvector, Redis 7.4; bound to 127.0.0.1; passwords from `.env`.
- Verified: unit + integration tests, ruff, mypy --strict, live API readiness check.

## Phase 1.2 — Core database and tenant isolation
- **Two database roles.** Migrations run as the owner (`MIGRATION_DATABASE_URL`). The API, worker and jobs connect as `voice_app`, which is not a superuser and cannot bypass row-level security. `uv run python -m voice_platform.cli db-setup` migrates and sets the app role's password.
- **Tables (migration `0001_foundation`):** `users` (global identity, citext email), `organizations` (tenant; slug, timezone, retention days), `roles`, `permissions`, `role_permissions` (seeded catalog), `memberships` (user × org × role), `audit_logs`, `outbox_events`.
- **Roles and permissions** (`tenancy/permissions.py`): OWNER, ADMIN, MANAGER, AGENT_MANAGER, ANALYST, VIEWER over 23 explicit permissions. No permission implies another (`calls.read` ≠ `calls.transcript.read`). Admin cannot delete the org or manage billing.
- **Row-level security** on `organizations`, `memberships`, `audit_logs`, `outbox_events`, forced for the table owner too. Policies read `app.org_id` / `app.user_id`, set per transaction by `tenant_transaction()` (`infrastructure/database.py`) with `set_config(..., true)` so values vanish at commit. No context = no rows.
  - A user sees the active org's members plus their own memberships in other orgs (for an org picker), never other users' memberships elsewhere.
- **Audit log is append-only:** the app role has no UPDATE/DELETE grant, and a trigger blocks changes even for the owner (only a retention job may purge, via an explicit flag).
- **Outbox:** events are written in the same transaction as the change; a dispatcher comes later.
- Verified (integration tests on a throwaway `<db>_test` database): models match migrations exactly; seeded catalog matches code; app role has no superuser/bypass; org A cannot read or write org B; no context sees nothing; context does not leak across pooled connections; audit log cannot be edited or deleted; migration downgrade → upgrade round trip.
- Note: IDs are UUIDv4 for now (docs proposed v7); can switch later without schema changes.
