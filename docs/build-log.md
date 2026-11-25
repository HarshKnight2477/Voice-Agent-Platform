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

## Phase 1.3 — Sign-up, login and role checks
- **Endpoints** (`api/auth.py`, `api/organizations.py`):
  `POST /api/v1/auth/signup` · `login` · `logout` · `switch-organization` · `verify-email` · `password-reset/request` · `password-reset/confirm`, `GET /api/v1/auth/sessions`, `DELETE /api/v1/auth/sessions/{id}`, `GET /api/v1/me`, `GET|PATCH /api/v1/organizations/current`, `GET /api/v1/organizations/current/members`.
- **Sign-up** creates the user, their organization and an OWNER membership in one transaction, writes audit entries and an `organization.created` outbox event, signs the user in and sends a verification link.
- **Passwords** (`identity/passwords.py`): Argon2id, 12–128 characters, not the email, not repetitive; old hashes upgraded on login. Unknown emails take the same time to reject as wrong passwords, and both get the same message.
- **Sessions** (migration `0002_auth_sessions`): random 256-bit token in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` outside local http); only its SHA-256 is stored. 14-day expiry, revocable, listed per user. The token is rotated when switching organization. A password reset signs the user out everywhere.
- **CSRF:** every state-changing request with a session must send `X-CSRF-Token`, an HMAC of the session token (returned at login and by `/me`, also in a readable `vp_csrf` cookie). Sign-up/login accept JSON only, which browsers cannot send cross-site without a CORS preflight.
- **Email tokens:** single-use, hashed, 48 h (verify) / 1 h (reset); only the newest reset link works. Reset requests answer identically whether or not the account exists. No email provider yet: locally the link is printed to the API console; staging/production refuse to start with that sender.
- **Rate limits** (Redis): sign-up 5/h per IP; login 50/15 min per IP and 10/15 min per email; reset 5/h per IP and 3/h per email. If Redis is down they fail open and log an error.
- **Authorization** (`api/dependencies.py`): tenant comes only from the session's active organization, re-checked against memberships on every request (a removed member loses access immediately). `require(Permission.X)` guards each endpoint and logs denials. Errors use one problem+json format (`api/errors.py`) and never echo submitted values.
- Fix found by tests: audit/outbox rows without an organization could not be inserted with `RETURNING` under row-level security; those models now set all defaults client-side.
- Verified: 49 tests (role matrix for all six roles, cross-organization switch blocked, CSRF required, session revocation, single-use tokens, reset signs out everywhere, rate limiting, audit + outbox written), ruff, mypy --strict, and a live sign-up → `/me` → PATCH-without-CSRF smoke test.
- Not yet: MFA (required for owners/admins later), invitations and role changes (next piece), a real email provider.

## Phase 1.4 — Team management and API conventions
- **Invitations** (migration `0003_invitations`, `api/members.py`): `POST /organizations/current/invitations` emails a 7-day single-use link; `GET` lists open ones; `DELETE …/{id}` revokes; `POST /api/v1/invitations/accept` joins. The token is `<org id>.<secret>` (only its hash is stored) and only the account whose email matches can accept; accepting also verifies that email. One open invitation per email per organization (partial unique index); row-level security on the table.
- **Role rules** (`tenancy/rules.py`, pure functions, tested over every role pair): only an OWNER grants OWNER or manages another OWNER; everyone else manages only lower-ranked members and grants at most their own rank; nobody changes their own role; an organization always keeps one OWNER (owner rows are locked while checking, so concurrent demotions can't both pass).
- **Members:** `GET /organizations/current/members` (paginated), `PATCH …/members/{user_id}` change role, `DELETE …/members/{user_id}` remove (or leave, for yourself). Removal clears the person's sessions from that organization at once.
- **Pagination** (`api/pagination.py`): keyset cursor on `(created_at, id)`, `limit` 1–100 (default 25), response `{data, next_cursor}`; invalid cursors give `400 invalid_cursor`.
- **Audit log API:** `GET /organizations/current/audit-logs` (needs `audit.read`), filterable by `action`. Role changes, removals and invitations are all audited.
- **HTTP hardening** (`shared/http_security.py`, `api/main.py`):
  - API-wide rate limit per signed-in session (or per IP when signed out), default 600 requests/minute, `429` + `Retry-After`; health checks are exempt.
  - Security headers on every response: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `CSP default-src 'none'` on the API, `Cache-Control: no-store` on `/api/*`, HSTS in staging/production.
  - CORS allows only the dashboard origin(s) (`CORS_ALLOWED_ORIGINS`, default `PUBLIC_APP_URL`) with credentials and the `X-CSRF-Token` header.
- **OpenAPI:** 19 documented paths at `/docs` (disabled in production).
- Verified: 104 tests (unit rule matrix; invitation accept/wrong account/forged token/duplicate/revoke; rank-limited promotion; self-change blocked; last-owner protection; removed member loses access; paginated audit log; viewer denied audit; security headers; CORS allow/deny; API rate limit), ruff, mypy --strict, live header check.

**Phase 1 (foundation) is complete.** Not included yet, by design: MFA, SSO, API keys, a real email provider (all scheduled for later phases).
