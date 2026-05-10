# Deployment

`odoo-mcp` is intentionally simple to deploy: one Python process per host
that talks XML-RPC to one or more Odoo instances. Pick the recipe that
matches your environment.

## 1. Local (development / single-user)

```bash
git clone https://github.com/molnkontakt/odoo-mcp.git
cd odoo-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cat > .env <<'EOF'
ODOO_DEV_URL=https://odoo-dev.example.com
ODOO_DEV_DB=odoo
ODOO_DEV_USER=user@example.com
ODOO_DEV_PASSWORD=replace-me

# Optional — production instance
ODOO_PROD_URL=https://odoo.example.com
ODOO_PROD_DB=odoo
ODOO_PROD_USER=user@example.com
ODOO_PROD_PASSWORD=replace-me

# Optional — audit-log destination (PostgreSQL)
MCP_AUDIT_DB_URL=postgresql://user:pass@host:5432/dbname

# Optional — pluggable validators (colon-separated module paths)
# MCP_VALIDATORS_PATH=my_validators.swedish_vat:my_validators.period_lock
EOF

# direnv / dotenv-cli / `set -a; source .env; set +a` — pick your poison
set -a; source .env; set +a
odoo-mcp
```

Wire into Claude Code:

```bash
claude mcp add odoo --transport stdio --command odoo-mcp
```

## 2. Systemd (always-on, single host)

Suitable when one Linux host hosts the MCP server and many MCP clients
reach it over a local socket or HTTP gateway.

`/etc/odoo-mcp/odoo-mcp.env`:

```ini
ODOO_PROD_URL=https://odoo.example.com
ODOO_PROD_DB=odoo
ODOO_PROD_USER=mcp-bot@example.com
ODOO_PROD_PASSWORD=...

ODOO_DEV_URL=https://odoo-dev.example.com
ODOO_DEV_DB=odoo
ODOO_DEV_USER=mcp-bot@example.com
ODOO_DEV_PASSWORD=...

MCP_AUDIT_DB_URL=postgresql://mcp_audit:...@db.internal/mcp_audit
```

`/etc/systemd/system/odoo-mcp.service`:

```ini
[Unit]
Description=Odoo MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=odoo-mcp
Group=odoo-mcp
EnvironmentFile=/etc/odoo-mcp/odoo-mcp.env
ExecStart=/opt/odoo-mcp/.venv/bin/odoo-mcp
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/log/odoo-mcp

[Install]
WantedBy=multi-user.target
```

Install:

```bash
sudo useradd --system --home /opt/odoo-mcp --shell /usr/sbin/nologin odoo-mcp
sudo install -d -o odoo-mcp -g odoo-mcp /opt/odoo-mcp /var/log/odoo-mcp
sudo -u odoo-mcp python3 -m venv /opt/odoo-mcp/.venv
sudo -u odoo-mcp /opt/odoo-mcp/.venv/bin/pip install --upgrade pip odoo-mcp

sudo install -d -m 750 -o root -g odoo-mcp /etc/odoo-mcp
sudo install -m 640 -o root -g odoo-mcp odoo-mcp.env /etc/odoo-mcp/

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-mcp
```

Verify:

```bash
sudo systemctl status odoo-mcp
sudo journalctl -u odoo-mcp -f
```

## 3. Container (Docker / Podman)

Minimal Dockerfile (will land in the repo in Phase 4):

```dockerfile
FROM python:3.12-slim
RUN useradd --system --create-home --uid 1000 odoo-mcp
USER odoo-mcp
WORKDIR /home/odoo-mcp
RUN pip install --user --no-cache-dir odoo-mcp
ENV PATH=/home/odoo-mcp/.local/bin:$PATH
ENTRYPOINT ["odoo-mcp"]
```

Run:

```bash
docker run --rm -i \
  -e ODOO_PROD_URL=https://odoo.example.com \
  -e ODOO_PROD_DB=odoo \
  -e ODOO_PROD_USER=mcp-bot@example.com \
  -e ODOO_PROD_PASSWORD="$ODOO_PROD_PASSWORD" \
  -e MCP_AUDIT_DB_URL="$MCP_AUDIT_DB_URL" \
  ghcr.io/molnkontakt/odoo-mcp:latest
```

Pass credentials via Docker secrets, Kubernetes secrets, or your
orchestrator's secret-manager — never bake them into the image.

## 4. HTTP transport (multi-user gateway)

When several MCP clients (Claude Code on different machines, Cursor in a
team, an internal AI tool) need to share one MCP server, run it behind a
streamable-HTTP gateway like
[mcp-supergateway](https://github.com/Shunsuke-Hoshino/mcp-supergateway).

The MCP server itself doesn't change — supergateway wraps a stdio MCP
process and exposes it as HTTP+SSE. Wire it in the gateway config:

```yaml
servers:
  odoo:
    type: stdio
    command: odoo-mcp
    env:
      ODOO_PROD_URL: ...
      ODOO_PROD_PASSWORD: ${ODOO_PROD_PASSWORD}
      MCP_AUDIT_DB_URL: ${MCP_AUDIT_DB_URL}
```

Clients connect to `https://gateway.example.com/odoo` instead of starting
their own subprocess.

## Audit-log database

If you set `MCP_AUDIT_DB_URL`, the server creates an `mcp_audit` table on
first connect:

```sql
CREATE TABLE mcp_audit (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT now(),
    session_id      TEXT,
    instance        TEXT,
    tool            TEXT NOT NULL,
    params          JSONB,
    response_summary TEXT,
    error           TEXT,
    duration_ms     INTEGER,
    idempotency_key TEXT
);
CREATE UNIQUE INDEX mcp_audit_idempotency_idx
    ON mcp_audit (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND error IS NULL;
```

A dedicated PostgreSQL user with `INSERT, SELECT, CREATE` on the target
database is enough — no superuser privileges required.

Useful queries:

```sql
-- What did the agent do today on prod?
SELECT ts, tool, response_summary
FROM mcp_audit
WHERE instance='prod' AND ts > now() - interval '24 hours' AND error IS NULL
ORDER BY ts DESC;

-- What failed?
SELECT ts, tool, error, params
FROM mcp_audit
WHERE error IS NOT NULL
ORDER BY ts DESC LIMIT 50;

-- Replay statistics for confirmed writes
SELECT tool, COUNT(*) AS total,
       COUNT(*) FILTER (WHERE idempotency_key IS NOT NULL) AS with_idempotency
FROM mcp_audit
WHERE tool LIKE 'odoo_post%' OR tool LIKE 'odoo_register_payment'
GROUP BY tool;
```

## Hardening checklist

- Use a dedicated **non-admin Odoo user**. Odoo's own ACLs become a
  defense-in-depth layer.
- Run the MCP server as a **non-root system user** (the systemd unit above
  does this).
- Mount credentials via **environment variables** populated from a secret
  manager — never commit them.
- Enable **TLS** on the Odoo XML-RPC endpoint (every Odoo nginx/Caddy
  recipe does this by default).
- Enable **audit logging** in production. Without it you have no record of
  what the agent did.
- Pass **idempotency keys** to write_critical tools so a re-issued
  `confirm=True` doesn't double-post.
- Keep `prod` and `dev` in **separate Odoo databases**, not just separate
  models — the validator framework treats them as fully isolated.

## Upgrades

```bash
# Local venv
pip install --upgrade odoo-mcp

# systemd
sudo -u odoo-mcp /opt/odoo-mcp/.venv/bin/pip install --upgrade odoo-mcp
sudo systemctl restart odoo-mcp

# Container
docker pull ghcr.io/molnkontakt/odoo-mcp:latest
```

Read [CHANGELOG.md](../CHANGELOG.md) (planned for Phase 4) before each
upgrade — write_critical tool semantics may evolve.
