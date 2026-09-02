# Architecture

## Overview

`odoo-mcp` is a thin layer between an MCP client (Claude Code, Cursor,
or any other MCP-compatible host) and one or more Odoo instances reached
over XML-RPC.

```
┌────────────────────┐   stdio/streamable HTTP   ┌──────────────────┐
│  MCP client        │ ──────────────────────────│  odoo-mcp        │
│  (Claude Code etc) │                           │  (server)        │
└────────────────────┘                           └────────┬─────────┘
                                                          │ XML-RPC (HTTPS)
                                                ┌─────────┴──────────┐
                                                │                    │
                                          ┌─────▼─────┐        ┌─────▼─────┐
                                          │ odoo prod │        │ odoo dev  │
                                          └───────────┘        └───────────┘
```

The server itself is stateless except for an in-memory cache of the
authenticated `uid` per instance. When configured, audit-log persistence is
delegated to an external PostgreSQL — see `MCP_AUDIT_DB_URL` below.

## Why XML-RPC

- Built into every self-hosted Odoo since v6 — no extra modules needed
- Simple request/response model fits MCP's tool-call model well
- Same protocol that Odoo's official Python `xmlrpc.client`-based examples use
- The newer JSON-RPC endpoint (`/web/dataset/call_kw`) requires session
  cookies and is harder to integrate cleanly across instances

## Multi-instance design

Every tool takes `instance: "prod" | "dev"` as its first parameter. The
server resolves URL + credentials from environment variables prefixed
with the uppercase instance name (`ODOO_PROD_URL`, `ODOO_DEV_URL`, …).

This lets the same MCP server serve both environments without a separate
process per instance, and lets the LLM explicitly pick its target.

## Client caching

`get_client(instance)` is `lru_cache`-d so repeated tool calls reuse the
same `OdooClient` and avoid re-authenticating each time. The cache holds
at most 2 entries (one per instance).

## Security model

### Three-tier tools

| Tier | Confirmation needed | Status | Examples |
|------|---------------------|--------|----------|
| **read** | Never | Shipped | `search_invoices`, `get_account_balance`, `query_account_aggregate` |
| **write_safe** | Auto (creates draft) | Shipped | `create_journal_entry_draft`, `add_tax_tags`, `set_partner` |
| **write_critical** | Requires `confirm=True` | Shipped | `post_journal_entry`, `register_payment`, `reverse_move` |

### Validation before writes

Each write tool runs its payload through a registry of validators. The
core ships three built-ins, all on by default:

- **BalanceValidator** — debit total must equal credit total (Decimal-safe)
- **AccountsExistValidator** — every referenced `account_code` resolves
- **TaxTagsExistValidator** — every `tag_code` resolves on the instance

External validators can be plugged in by setting `MCP_VALIDATORS_PATH` to
a colon-separated list of importable Python modules. Each module must
expose `register(registry)` and may add its own validators (e.g. Swedish
VAT one-sided reverse charge, period locks against submitted VAT returns,
etc.). The core stays domain-agnostic.

### Audit log

When `MCP_AUDIT_DB_URL` is set in the environment, every `write_safe` and
`write_critical` tool call records a row to a `mcp_audit` table on that
PostgreSQL instance. The table is auto-created on first connect:

```sql
CREATE TABLE mcp_audit (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT now(),
    session_id      TEXT,           -- MCP session id from the host
    instance        TEXT NOT NULL,  -- "prod" or "dev"
    tool            TEXT NOT NULL,
    params          JSONB,
    response_summary TEXT,          -- e.g. "created move id=3960"
    error           TEXT,
    duration_ms     INTEGER,
    idempotency_key TEXT
);
CREATE UNIQUE INDEX mcp_audit_idempotency_idx
    ON mcp_audit (instance, tool, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND error IS NULL;
```

Read tools are not audit-logged today (they don't change state). When
`MCP_AUDIT_DB_URL` is unset, the audit logger is a silent no-op so
local development needs no Postgres.

#### Idempotency

`write_critical` tools accept an `idempotency_key`. If audit-log is
enabled and a row with that key already exists with `error IS NULL`, the
tool short-circuits and returns the previous summary instead of acting
again. Lets clients safely retry transient transport errors without
double-posting or double-charging.

### Auth

There are **two** auth boundaries, and they do not share a credential:

```
MCP client ──OAuth 2.1 (Authentik)──▶ odoo-mcp ──Odoo API key──▶ Odoo
           ▲ per-user, per-scope              ▲ one service account
```

**Client → server.** Only for the HTTP transport. The server is a *resource
server*: it verifies JWTs against the IdP's JWKS (`auth.py`), advertises RFC
9728 Protected Resource Metadata, and answers unauthenticated requests with a
`WWW-Authenticate` challenge carrying `resource_metadata` — the pair that lets
a web client discover where to log in.

Two modes, because MCP clients expect to self-register:

- `MCP_AUTH_MODE=oauth` — resource server only. The caller already holds a
  token issued by the IdP.
- `MCP_AUTH_MODE=oauth-proxy` — additionally fronts the IdP with an RFC 7591
  Dynamic Client Registration endpoint, since Authentik has none outside its
  enterprise tier. The proxy performs the upstream flow with this server's own
  pre-registered credentials, so login and consent remain Authentik's, and
  client redirect URIs are matched against an allow-list rather than accepted
  as given. The token handed back is still the IdP's JWT; no new issuer.

The stdio transport has no OAuth: its trust boundary is the local process, and
`check_scopes` is a no-op there.

**Server → Odoo.** Always an Odoo credential (`instances.py`), because Odoo's
`auth_oidc` is web-login only — an Authentik access token is not accepted by
XML-RPC. An Odoo **API key** is a drop-in replacement for the password in
`common.authenticate` / `execute_kw`, so this hop should use one, on a
dedicated non-admin user, so Odoo's own ACLs act as a second layer.

Because that account is shared by every caller, Odoo's `create_uid`/`write_uid`
cannot attribute anything. The `mcp_audit` row is the only attribution, and it
takes `actor` / `actor_sub` / `client_id` from the verified token — never from
a tool argument, which would be a caller-supplied audit trail.

#### Scopes

| Scope | Grants | Implies |
|---|---|---|
| `odoo:read` | read tier | — |
| `odoo:write` | write_safe tier | `odoo:read` |
| `odoo:critical` | write_critical tier | `odoo:write` |
| `odoo:prod` | `instance="prod"` on any tier | — |

`odoo:prod` sits outside the tier chain on purpose: the tier is *what kind of
call*, `odoo:prod` is *which ledger*.

Enforcement is a `@requires_scope(...)` decorator applied under `@mcp.tool()`,
so the check is part of the tool function and also fires when the function is
called directly. A scope helper with no call sites is decoration, not
protection.

## Tool catalog

See [TOOLS.md](TOOLS.md) for the complete reference per shipped/planned tier.

## Roadmap

### Phase 1 — Read-only ✅ Shipped
- FastMCP skeleton + XML-RPC client
- `odoo_search_partners`, `odoo_get_partner`, `odoo_search_invoices`,
  `odoo_search_journal_entries`, `odoo_get_invoice`,
  `odoo_get_account_balance`, `odoo_query_account_aggregate`

### Phase 2 — Write safe + audit log ✅ Shipped
- `odoo_create_journal_entry_draft`, `odoo_add_tax_tags`, `odoo_set_partner`
- Validator framework (Balance, AccountsExist, TaxTagsExist) + plugin loader
- Audit-log infrastructure with no-op fallback
- Test suite (pytest + MockClient)

### Phase 3 — Write critical + Hosting ✅ Shipped
- `odoo_post_journal_entry` + `odoo_register_payment` with `confirm=True`
- Post-time validators (`PostStateValidator`, `PostBalanceValidator`)
- Audit-log coverage for critical writes + idempotency keys
- Deploy documentation (systemd, Docker, HTTP gateway)

### Phase 4 — Polish (planned)
- Container image
- Integration test suite against an ephemeral Odoo Docker
- CHANGELOG template + release automation

## Open questions

- **Multi-session isolation:** should the dev instance always be accessible
  without confirm, or always require confirm? (Current plan: no confirm
  on dev, confirm on prod for write_critical)
- **Rate limiting:** is it needed?
- **Dry-run mode** for write tools? Return a summary of what would have
  happened without actually doing it.
