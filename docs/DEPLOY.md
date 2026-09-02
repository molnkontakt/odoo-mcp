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

# Audit log (PostgreSQL). Required in practice: critical writes fail closed
# without it. See "Audit-log database" below.
MCP_AUDIT_DB_URL=postgresql://user:pass@host:5432/dbname?sslmode=require

# Local development only — let the server create its own audit schema.
# MCP_AUDIT_AUTO_DDL=1

# Local development only — allow post/payment/reversal with no audit log.
# MCP_ALLOW_UNAUDITED_CRITICAL_WRITES=1

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

MCP_AUDIT_DB_URL=postgresql://USER:PASSWORD@HOST:5432/DBNAME
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

> [!warning] Never `pip install odoo-mcp`
> That name on PyPI belongs to an **unrelated** project (`erpipe-org/mcp-odoo`,
> currently 1.3.0). Installing or upgrading it would replace this server with
> third-party code that inherits live Odoo credentials — and because the local
> version is 0.1.0, `--upgrade` resolves to *their* release and wins.
> This distribution is named `molnkontakt-odoo-mcp` and is **not published to
> PyPI**. Always install from an explicit git ref, never a bare package name.

```bash
sudo useradd --system --home /opt/odoo-mcp --shell /usr/sbin/nologin odoo-mcp
sudo install -d -o odoo-mcp -g odoo-mcp /opt/odoo-mcp /var/log/odoo-mcp
sudo -u odoo-mcp python3 -m venv /opt/odoo-mcp/.venv
sudo -u odoo-mcp /opt/odoo-mcp/.venv/bin/pip install --upgrade pip
sudo -u odoo-mcp /opt/odoo-mcp/.venv/bin/pip install \
  "molnkontakt-odoo-mcp @ git+https://github.com/molnkontakt/odoo-mcp.git@<commit-sha>"

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

## 4. Remote (Streamable HTTP + OAuth)

This is the mode that lets **web-based MCP clients** — claude.ai custom
connectors, Claude Desktop, ChatGPT connectors — reach the server. One
long-lived process serves many callers, and every caller is a verified OAuth
identity rather than "whoever can open the port".

> [!danger] The HTTP transport refuses to start without token verification
> `MCP_AUTH_MODE=none` + `MCP_TRANSPORT=http` is an anonymous, internet-facing
> accounting API — `odoo_post_journal_entry` and `odoo_register_payment`
> included. The server exits with an explanation instead of starting.
> `MCP_ALLOW_UNAUTHENTICATED_HTTP=1` exists for loopback testing and is named
> so it cannot be mistaken for a production setting.
>
> An earlier version of this document recommended wrapping the stdio process in
> a generic HTTP gateway. Don't: that puts an unauthenticated MCP server on a
> network port and makes every audit row anonymous. Use this section instead.

### Two auth boundaries

```
client ──OAuth (Authentik)──▶ odoo-mcp ──Odoo API key──▶ Odoo
        ▲ per-user identity            ▲ one service account
```

OAuth gates **client → server** only. Odoo will not accept an Authentik token
for XML-RPC (`auth_oidc` is web-login only), so the **server → Odoo** hop keeps
using one minimally scoped Odoo service account. Consequence: Odoo's own
`create_uid`/`write_uid` shows that service account for every action, and the
`mcp_audit` row — `actor`, `actor_sub`, `client_id`, taken from the verified
token, never from a tool argument — is the *only* record of which human acted.

Per-user Odoo API keys were considered and rejected: one server holding every
accountant's key is a confused deputy that makes Odoo-side attribution look
real while the server can still act as anyone.

### Scopes

| Scope | Grants | Implies |
|---|---|---|
| `odoo:read` | every tool in the read tier | — |
| `odoo:write` | draft-creating tools (`create_*`, `update_*`, `add_tax_tags`) | `odoo:read` |
| `odoo:critical` | `post_journal_entry`, `register_payment`, `reverse_move` | `odoo:write` |
| `odoo:prod` | acting on `instance="prod"` at all | — |

`odoo:prod` is deliberately outside the tier chain: the tier says *what kind of
call*, `odoo:prod` says *which ledger*. A client can be trusted to post entries
in dev without being trusted to post them in prod.

Scopes are enforced in the tool functions themselves (`@requires_scope`), not
only advertised in metadata — a scope check with no call sites reads as
protection while granting everything.

### Authentik provider

In Authentik, create an **OAuth2/OpenID Provider** + Application:

| Setting | Value |
|---|---|
| Client type | Confidential — the OAuth client is *this server*, which can keep a secret |
| Redirect URI | `https://<public-url>/auth/callback` (strict) — the proxy's own callback, not the MCP client's |
| Grant types | `authorization_code`, `refresh_token` — **set them explicitly**; Authentik's API creates providers with an empty list, which rejects every authorize call before the login screen |
| Scopes | the four `odoo:*` scopes as custom scope mappings, plus `openid`, `profile`, `email`, `offline_access` |
| Sub mode | Based on the user's UUID — stable, unlike an email |
| Issuer mode | Per provider |
| Signing key | any RSA key; Authentik then issues JWT access tokens |

Bind the application to a group. An unbound Authentik application is reachable
by every user who can log in.

> [!important] Audience comes from a scope mapping
> Authentik has no `audience` field on the provider — by default `aud` is the
> client ID. Give each `odoo:*` scope mapping an expression that sets it:
>
> ```python
> return {"aud": "https://<public-url>/mcp"}
> ```
>
> Then `MCP_OAUTH_AUDIENCE` is that same URL. Verify on the first real login
> (decode the access token) — if `aud` still holds the client ID, either fix
> the mapping or list both values in `MCP_OAUTH_AUDIENCE` (comma-separated).
> Without audience binding, a token minted for another Authentik application is
> accepted here.

### Which auth mode: `oauth` or `oauth-proxy`

MCP clients (Claude Code, Claude Desktop, claude.ai connectors) expect to
register themselves through **RFC 7591 Dynamic Client Registration**. Authentik
has no `registration_endpoint` outside its enterprise tier, so those clients
cannot complete a flow against it directly.

| Mode | Use when | What the client talks to |
|---|---|---|
| `oauth` | the caller already holds a token for this resource (a service, or a client you pre-registered by hand) | Authentik directly |
| `oauth-proxy` | Claude Code / Claude Desktop / claude.ai connectors | this server, which runs the real flow upstream with its own credentials |

In proxy mode this server accepts the DCR call and then performs the
authorization-code flow against Authentik using its pre-registered client. Two
protections stay in place, and both were missing from a sibling MCP server in
this estate whose DCR endpoint approved every client with no login and no
consent:

- the upstream login and consent are **Authentik's**, so the IdP still decides
  who may log in at all;
- client redirect URIs are matched against an allow-list
  (`MCP_OAUTH_CLIENT_REDIRECT_URIS`, defaulting to the Claude callbacks plus
  loopback). FastMCP's own default is "any redirect URI", which is an open
  redirector for the authorization code.

The token the client ends up holding is still an Authentik-signed JWT, verified
here exactly as in `oauth` mode. The proxy adds no new token issuer.

### Configuration

```ini
MCP_TRANSPORT=http
MCP_HTTP_HOST=127.0.0.1
MCP_HTTP_PORT=8000
MCP_HTTP_PATH=/mcp

MCP_AUTH_MODE=oauth-proxy
MCP_PUBLIC_URL=https://odoo-mcp.example.com
MCP_OAUTH_ISSUER=https://auth.example.com/application/o/odoo-mcp/
MCP_OAUTH_AUDIENCE=https://odoo-mcp.example.com/mcp
MCP_OAUTH_CLIENT_ID=...            # from the Authentik provider
MCP_OAUTH_CLIENT_SECRET=...        # keep in a secret manager, never in the unit file
# Optional: skips OIDC discovery at startup (plain `oauth` mode only)
# MCP_OAUTH_JWKS_URI=https://auth.example.com/application/o/odoo-mcp/jwks/
# Optional: override the client redirect allow-list (comma-separated, wildcards ok)
# MCP_OAUTH_CLIENT_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,http://localhost:*
```

No extra dependencies: `authlib` (JWT/JWKS) and `uvicorn` ship with `fastmcp`.

Bind to loopback and terminate TLS in front of the process — the server speaks
plain HTTP and trusts nothing about the network it sits on. Reuse the systemd
unit in section 2 as-is; only the environment file changes.

> [!note] Serving under a path prefix
> With `MCP_PUBLIC_URL=https://host/odoo` the server advertises every endpoint
> under `/odoo/…` but still *serves* them at the root of its own port, except
> the resource metadata, which it serves at the full
> `/.well-known/oauth-protected-resource/odoo/mcp`. A reverse proxy therefore
> needs three routes: strip the prefix for `/odoo/*`, pass
> `/.well-known/oauth-protected-resource/odoo/*` through unchanged, and rewrite
> `/.well-known/oauth-authorization-server/odoo` to
> `/.well-known/oauth-authorization-server`.

### Verify

```bash
# Protected resource metadata (RFC 9728) — this is what a web client reads
curl -s https://odoo-mcp.example.com/.well-known/oauth-protected-resource/mcp | jq

# An unauthenticated call must be 401 *and* carry resource_metadata, or the
# client has no way to discover the authorization server
curl -si -X POST https://odoo-mcp.example.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | grep -i www-authenticate
```

Expected challenge:

```
www-authenticate: Bearer error="invalid_token", ...,
  resource_metadata="https://odoo-mcp.example.com/.well-known/oauth-protected-resource/mcp"
```

### Connecting a client

- **claude.ai** → Settings → Connectors → *Add custom connector* →
  `https://odoo-mcp.example.com/mcp`. The OAuth flow is discovered from the
  metadata above.
- **Claude Desktop** → Settings → Connectors → same URL.
- **Claude Code** → `claude mcp add odoo --transport http https://odoo-mcp.example.com/mcp`

Stdio stays supported and unchanged for local single-user setups; it is still
the default (`MCP_TRANSPORT=stdio`, no OAuth, audit rows attributed to
`local:stdio`).

## Audit-log database

This table is the only record of *who* did *what*. Odoo's own history shows the
shared service account for every action, so once a service credential is used,
this log is the sole attribution source. Treat it as a bookkeeping artifact
(in Sweden, BFL retention), not as telemetry.

> [!important] Provision the schema out of band
> The server **verifies** that `mcp_audit` exists; it does not create it. The
> production role deliberately has no `CREATE` on the schema — a process that
> can create its own audit table can also replace it, and the table is the
> evidence about that very process. Set `MCP_AUDIT_AUTO_DDL=1` only for local
> development, where the role owns its own throwaway database.

```sql
CREATE TABLE mcp_audit (
    id                 BIGSERIAL PRIMARY KEY,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_ts       TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'in_progress',
    session_id         TEXT,
    instance           TEXT,
    ledger             TEXT,
    tool               TEXT NOT NULL,
    params             JSONB,
    params_fingerprint TEXT,
    response_summary   TEXT,
    error              TEXT,
    duration_ms        INTEGER,
    idempotency_key    TEXT,
    actor              TEXT,
    actor_sub          TEXT,
    client_id          TEXT,
    CONSTRAINT mcp_audit_status_chk
        CHECK (status IN ('in_progress','committed','ok','error','unknown'))
);

-- Scoped to (instance, tool, key) so a key reused across environments or
-- tools cannot short-circuit the wrong call. Deliberately NOT filtered on
-- status: a call that failed after the request hit the wire may have been
-- applied, so its key must stay burnt rather than re-enabling a retry.
CREATE UNIQUE INDEX mcp_audit_idempotency_idx
    ON mcp_audit (instance, tool, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
```

### Statuses

| Status | Meaning | Blocks a replay |
|---|---|---|
| `in_progress` | claimed, call not finished (or the process died) | yes |
| `committed` | Odoo did the work; something after it failed | yes |
| `ok` | completed cleanly | yes |
| `unknown` | transport error — the request may have reached Odoo | yes |
| `error` | failed before Odoo could act | no |

The row is claimed **before** the Odoo call and completed after. That ordering
is what makes idempotency real: the unique index rejects a duplicate at the
database instead of in a check-then-act race, and a process that dies mid-call
leaves an `in_progress` row that both preserves the evidence and keeps the key
burnt.

### Grants

The role needs `SELECT, INSERT` plus a **narrow** `UPDATE` on the completion
columns only — never `DELETE`, and never `UPDATE` on the attribution or request
columns:

```sql
GRANT SELECT, INSERT ON mcp_audit TO odoo_mcp;
GRANT UPDATE (status, completed_ts, response_summary, error, duration_ms)
    ON mcp_audit TO odoo_mcp;
GRANT USAGE, SELECT ON SEQUENCE mcp_audit_id_seq TO odoo_mcp;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO odoo_mcp;
```

Back this with `BEFORE UPDATE` / `BEFORE DELETE` triggers that reject terminal-row
edits and any DELETE, so a future `GRANT` mistake cannot quietly open it up.

### Fail-closed

Critical writes refuse to run when this log is unreachable, rather than acting
unrecorded. `MCP_ALLOW_UNAUDITED_CRITICAL_WRITES=1` opts out — local
development only. Note the consequence: if the audit database is down, posting,
payments and reversals stop. That is the intended trade.

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
WHERE tool LIKE 'odoo_post%' OR tool = 'odoo_register_payment'
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

A `CHANGELOG.md` is planned for Phase 4 and is not yet available. Until
then, review release notes/commit history before each upgrade —
`write_critical` tool semantics may evolve.
