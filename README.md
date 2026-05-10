# odoo-mcp

MCP server for Odoo. Gives Claude (or any other MCP client) controlled
access to one or more Odoo instances via XML-RPC. Built for
multi-environment setups (typically prod + dev).

**Status:** Phases 1–3 are implemented (read tools, write_safe + audit log,
write_critical with confirm + idempotency). Phase 4 polish (CHANGELOG,
container image, integration tests) is planned —
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start

```bash
git clone https://github.com/molnkontakt/odoo-mcp.git
cd odoo-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Set credentials in the environment (or a .env file via direnv/dotenv-cli)
export ODOO_DEV_URL=https://odoo-dev.example.com
export ODOO_DEV_DB=odoo
export ODOO_DEV_USER=user@example.com
export ODOO_DEV_PASSWORD=...

# Optional — turn on audit logging
export MCP_AUDIT_DB_URL=postgresql://user:pass@host/dbname

# Run locally (stdio transport)
odoo-mcp
```

## Wire it into Claude Code

```bash
claude mcp add odoo --transport stdio --command odoo-mcp
```

Or expose it over HTTP via a gateway/proxy in production.

## Tools

See [docs/TOOLS.md](docs/TOOLS.md) for the complete reference.

Three tiers:

- **read**: free, no confirmation, not audit-logged
- **write_safe**: creates drafts only, audit-logged when configured,
  validated against built-in and pluggable rules
- **write_critical**: requires `confirm=True`. First call without `confirm`
  returns a preview + validator outcome so the user can sanity-check
  before authorizing. Optional `idempotency_key` for replay-safety.

## Validators

Each write tool runs its payload through a registry of validators before
calling Odoo. Built-ins (always on):

- **Balance** — debit total equals credit total (Decimal-precise)
- **AccountsExist** — every `account_code` resolves on the instance
- **TaxTagsExist** — every tag code resolves on the instance

Plug in domain-specific validators via the `MCP_VALIDATORS_PATH` env var
(colon-separated list of importable Python modules). Each module's
`register(registry)` is called at startup.

## Audit logging

Set `MCP_AUDIT_DB_URL` to a PostgreSQL URL to record every write_safe
call to a `mcp_audit` table (auto-created). Read tools are not logged.
When the env var is absent, audit logging silently no-ops — no Postgres
needed for local development.

## Requirements

- Python 3.11+
- FastMCP
- Odoo 16+ with XML-RPC enabled (default on all self-hosted installations)
- (Optional) PostgreSQL for audit logging
- (Optional) A secret manager such as Phase or Vault for credentials

## License

LGPL-3
