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
authenticated `uid` per instance. All persistence (audit log, request
deduplication for confirmed writes) is delegated to an external
PostgreSQL.

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

| Tier | Confirmation needed | Examples |
|------|---------------------|----------|
| **read** | Never | `search_invoices`, `get_account_balance`, `query_account_aggregate` |
| **write_safe** | Auto (creates draft) | `create_journal_entry_draft`, `create_partner` |
| **write_critical** | Requires `confirm=True` + audit | `post_journal_entry`, `unlink_move`, `update_posted_invoice` |

### Validation before writes

Before a `write_critical` tool accepts its payload, domain-specific
validations run (pluggable):

- **Balance:** debit ≠ credit is rejected (Odoo enforces this anyway, but
  we surface a clearer error earlier in the flow)
- **Domain-specific rules:** load validators from a configurable plugin
  directory (e.g. Swedish VAT validators like "one-sided reverse charge",
  period lock against submitted VAT returns, etc.) — the core stays
  domain-agnostic

### Audit log

Every tool call (read AND write) is logged to a configurable PostgreSQL
instance:

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
    duration_ms     INTEGER
);
```

### Auth

- Odoo credentials read from environment variables (typically populated by
  a secret manager such as Phase, Vault, AWS SSM, or a `.env` file locally)
- Recommendation: use a dedicated non-admin user so Odoo's own permission
  controls act as an additional safety layer

## Tool catalog (planned)

### Read

- `odoo_search_partners(instance, query, limit?)` — ilike on name/VAT
- `odoo_get_partner(instance, partner_id)` — full info incl. country, VAT
- `odoo_search_invoices(instance, date_from, date_to, partner_id?, state?, move_type?)`
- `odoo_get_invoice(instance, move_id)` — header + lines + payments
- `odoo_get_account_balance(instance, account_code, date_from?, date_to?)`
- `odoo_search_journal_entries(instance, ref?, date_from?, date_to?, state?)`
- `odoo_query_account_aggregate(instance, account_codes, date_from, date_to, group_by?)`
  — generic multi-account sum, useful for building reports client-side

### Write — safe (creates drafts)

- `odoo_create_journal_entry_draft(instance, date, ref, lines)`
  — `lines: [{account_code, debit, credit, name, tax_tag_codes?, partner_id?}]`
- `odoo_add_tax_tags(instance, line_ids, tag_codes)`
- `odoo_set_partner(instance, move_id, partner_id)`

### Write — critical (requires confirm)

- `odoo_post_journal_entry(instance, move_id, confirm=True)`
- `odoo_register_payment(instance, move_id, journal_id, amount, confirm=True)`

### No write-only-direct (always create draft, then post separately)

Design principle: never a single tool that both creates AND posts in the
same call. Forces the LLM to pause and show the draft to the user before
posting.

## Roadmap

### Phase 1 — MVP (read-only) ✅
- FastMCP skeleton + XML-RPC client
- All `odoo_*search/get` tools
- Local stdio testing against dev

### Phase 2 — Write safe
- `create_journal_entry_draft` + `add_tax_tags` + `set_partner`
- Validation before create (balance, account codes exist, tags exist)
- Audit table + insert helper (PostgreSQL)

### Phase 3 — Write critical + Hosting
- `post_journal_entry` + `register_payment` with confirm pattern
- Pluggable validators
- Deploy documentation (systemd, Docker, supergateway)

### Phase 4 — Polish
- README + tool docstrings
- Test suite (pytest against mock-Odoo + integration tests against dev)
- CHANGELOG template

## Open questions

- **Multi-session isolation:** should the dev instance always be accessible
  without confirm, or always require confirm? (Current plan: no confirm
  on dev, confirm on prod for write_critical)
- **Rate limiting:** is it needed?
- **Dry-run mode** for write tools? Return a summary of what would have
  happened without actually doing it.
- **Tool output:** return structured data + human-readable summary, or
  only structured? LLMs communicate better when there is summary text
  available.
