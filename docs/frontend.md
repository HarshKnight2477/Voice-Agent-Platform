# Frontend Architecture

Next.js (App Router) + TypeScript (strict) + Tailwind + shadcn/ui (Radix, accessible) + TanStack Query + typed client generated from the API's OpenAPI spec. LiveKit browser SDK (`livekit-client`, `@livekit/components-react`) only in the agent test console.

## Areas
- **Auth**: sign up, sign in, MFA, reset, invitations.
- **Dashboard**: calls today/month, success rate, transfers, avg duration, avg latency, AI + telephony cost, recent calls, agent health, alerts; filters (agent, date, outcome, intent, number).
- **Agents**: list, wizard (11 steps, progressive disclosure, Basic/Advanced), configuration tabs, versions (diff, publish, rollback), test console (browser call + scripted tests).
- **Knowledge**: bases, uploads, status, test query.
- **Phone numbers & carriers**: BYOC setup wizard, number assignment, routing, business hours.
- **Calls**: list + **Call Inspector** (transcript, recording if permitted, timeline STT→LLM→tool→TTS with latency bars, tool calls/results, workflow state, errors, cost, AI summary).
- **Live monitor** (post-MVP), **Integrations**, **Settings** (org, members/roles, retention, API keys, billing).

## Rules
- Server is the only authority; UI hides what the role can't do but never relies on it.
- No tokens in localStorage; cookie sessions + CSRF header.
- Light/dark, keyboard navigation, WCAG 2.2 AA, responsive, skeletons, empty/error states, confirm dialogs for destructive actions.
- Plain business language (no "VAD", "STT" in Basic mode).
