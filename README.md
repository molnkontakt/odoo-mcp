# odoo-mcp

MCP server for Odoo. Gives Claude (or any other MCP client) controlled,
audit-logged access to one or more Odoo instances via XML-RPC. Built for
multi-environment setups (typically prod + dev).

**Status:** Phase 1 (read-only tools) is implemented. Phase 2 (safe writes)
and Phase 3 (critical writes + audit log) are planned — see
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

- **read**: free, no confirmation
- **write_safe**: creates drafts, no confirmation but audit-logged
- **write_critical**: requires `confirm=True`, validated against pluggable
  domain rules

## Requirements

- Python 3.11+
- FastMCP
- Odoo 16+ with XML-RPC enabled (default on all self-hosted installations)
- (Optional) A secret manager such as Phase or Vault for credentials

## License

LGPL-3
