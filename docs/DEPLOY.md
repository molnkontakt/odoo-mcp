# Deployment

> **Status:** Draft — full deploy recipes will land in Phase 3.

## Local (development)

```bash
git clone https://github.com/molnkontakt/odoo-mcp.git
cd odoo-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure credentials in a .env file (gitignored)
cat > .env <<'EOF'
ODOO_DEV_URL=https://odoo-dev.example.com
ODOO_DEV_DB=odoo
ODOO_DEV_USER=user@example.com
ODOO_DEV_PASSWORD=replace-me
EOF

# Then either:
#   set -a; source .env; set +a; odoo-mcp
# or use direnv / dotenv-cli.
```

Wire it into Claude Code:

```bash
claude mcp add odoo --transport stdio --command odoo-mcp
```

## Production (planned, Phase 3)

The intended target is a small Linux host with two services:

- `odoo-mcp.service` — systemd unit running `odoo-mcp` as a long-lived
  process behind a streamable HTTP transport
- `mcp-supergateway` (optional) — gateway that fan-outs to multiple MCP
  servers, exposes one HTTPS endpoint

Both are stateless: state lives in the audit-log PostgreSQL.

Detailed unit files, secret-manager wiring, and reverse-proxy config will
be documented here once the audit/validator infrastructure (Phase 2/3)
is implemented.

## Container image (planned)

A `Dockerfile` will be added in Phase 4. The image will:

- Be based on `python:3.12-slim`
- Run as a non-root user
- Read all credentials via env (12-factor)
- Expose port 8765 for streamable HTTP transport (configurable)
