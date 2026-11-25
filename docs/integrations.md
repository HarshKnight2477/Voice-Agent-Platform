# Integrations Framework

## Model
`Integration` (tenant, type, status, granted scopes) + encrypted `IntegrationCredential` + a `Connector` implementation exposing **capabilities** that tools call (e.g. `calendar.find_slots`, `crm.upsert_contact`). Tools depend on capabilities, not vendors, so a tenant can swap Google Calendar for Microsoft Calendar without changing agents.

Each connector implements: `authorize()` (OAuth or API key), `test_connection()`, capability methods with timeouts, rate-limit awareness, error mapping, audit hooks. Credentials are decrypted only inside the executing job/worker for the duration of the call.

## Planned connectors
MVP: built-in reservations/appointments store, SMS confirmation (provider TBD), email (SMTP/provider). Later: Google Calendar, Microsoft 365 Calendar, HubSpot, Salesforce, Zoho, Shopify, restaurant POS/booking systems, custom REST (with egress guard, response schema, allow-listed hosts).

## MCP
- Only platform-approved MCP servers (`mcp_servers` table) can be enabled per tenant; tenants pick allowed tools.
- The worker connects via LiveKit `mcp.MCPToolset` **inside the gateway**, not as raw model tools: each MCP tool is re-registered as a gateway tool with our schema copy, permissions, rate limit, timeout and audit.
- MCP tool descriptions and results are untrusted: descriptions are frozen at approval time (changes require re-approval), results sanitised and size-capped.
- Auth headers come from encrypted credentials; never from model output.
