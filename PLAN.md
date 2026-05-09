# odoo-mcp — Plan

MCP-server som ger Claude kontrollerat, auditbart access till en Odoo-instans
via XML-RPC. Designad för att ersätta direkta `odoo shell`-anrop som ofta
blockeras av AI-sandboxes.

## Arkitektur

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

- **Språk:** Python 3.11+ med [FastMCP](https://github.com/jlowin/fastmcp)
- **Odoo-protokoll:** XML-RPC mot `/xmlrpc/2/object` och `/xmlrpc/2/common`
- **Multi-instans:** varje verktyg tar `instance: "prod"|"dev"` som första parameter
- **Hosting:** själv-hostad, t.ex. som systemd-service med stdio eller streamable HTTP transport

## Säkerhetsmodell

### Three-tier verktyg

| Tier | Behov av confirm | Exempel |
|------|------------------|---------|
| **read** | Aldrig | `search_invoices`, `get_account_balance`, `query_vat_report` |
| **write_safe** | Auto (skapar utkast) | `create_journal_entry_draft`, `create_partner` |
| **write_critical** | Kräver `confirm=True` + audit | `post_journal_entry`, `unlink_move`, `update_posted_invoice` |

### Validering före writes

Innan en `write_critical`-tool accepterar payload körs domänspecifika
valideringar (pluggable):

- **Balans:** debet ≠ kredit blockeras (Odoo gör redan, men vi felmeddelar
  tidigare med tydligare felmeddelande)
- **Domain-specific rules:** ladda valideringar från en konfigurerbar plugin-katalog
  (t.ex. svensk-moms-validatorer som "one-sided reverse charge", periodlås mot
  inlämnad momsdeklaration osv.) — domän-agnostisk i kärnan

### Auditlogg

Varje verktygskall (read OCH write) loggas till en konfigurerbar PostgreSQL-instans:

```sql
CREATE TABLE mcp_audit (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT now(),
    session_id      TEXT,           -- Claude session från MCP-context
    instance        TEXT NOT NULL,  -- "prod" eller "dev"
    tool            TEXT NOT NULL,
    params          JSONB,
    response_summary TEXT,          -- t.ex. "created move id=3960"
    error           TEXT,
    duration_ms     INTEGER
);
```

### Auth

- Odoo-credentials läses från environment variables (typiskt populerade av
  en secret-manager som Phase, Vault, AWS SSM, eller `.env` lokalt)
- Rekommendation: använd en dedikerad icke-admin-användare så Odoos egna
  behörighetskontroller fungerar som ytterligare skyddslager

## Verktyg (v1 scope)

### Read

- `odoo_search_partners(instance, query, limit?)` — ilike på namn/VAT
- `odoo_get_partner(instance, partner_id)` — full info inkl. country, VAT
- `odoo_search_invoices(instance, date_from, date_to, partner_id?, state?, move_type?)`
- `odoo_get_invoice(instance, move_id)` — header + lines + payments
- `odoo_get_account_balance(instance, account_code, date_from?, date_to?)`
- `odoo_search_journal_entries(instance, ref?, date_from?, date_to?, state?)`
- `odoo_query_account_aggregate(instance, account_codes, date_from, date_to, group_by?)`
  — generisk multi-konto-summa, useful för att bygga rapporter från klient-sidan

### Write — safe (skapar utkast)

- `odoo_create_journal_entry_draft(instance, date, ref, lines)`
  — `lines: [{account_code, debit, credit, name, tax_tag_codes?, partner_id?}]`
- `odoo_add_tax_tags(instance, line_ids, tag_codes)`
- `odoo_set_partner(instance, move_id, partner_id)`

### Write — critical (kräver confirm)

- `odoo_post_journal_entry(instance, move_id, confirm=True)`
- `odoo_register_payment(instance, move_id, journal_id, amount, confirm=True)`

### Inga write-only-direct (lägg som draft + posta separat)

Designprincip: aldrig en single-tool som både skapar OCH postar i samma
anrop. Tvingar Claude att stanna upp och visa utkastet innan posting.

## Utvecklingsfaser

### Fas 1 — MVP (read-only)
- FastMCP-skeleton + XML-RPC-klient
- Implementera alla `odoo_*search/get`-verktyg
- Lokal stdio-test mot dev
- **Estimat:** 4–6 timmar

### Fas 2 — Write safe
- `create_journal_entry_draft` + `add_tax_tags` + `set_partner`
- Validering före create (balans, account-codes finns, tags finns)
- Audit-tabell + insert-helper (PostgreSQL)
- **Estimat:** 4 timmar

### Fas 3 — Write critical + Hosting
- `post_journal_entry` + `register_payment` med confirm-pattern
- Pluggable validators
- Deploy-dokumentation (systemd, Docker, supergateway)
- **Estimat:** 4 timmar

### Fas 4 — Polish
- README + tool docstrings
- Test-suite (pytest mot mock-Odoo + integration-tester mot dev)
- CHANGELOG-template
- **Estimat:** 2 timmar

**Totalt:** ~15 timmar utveckling.

## Repo-struktur

```
odoo-mcp/
├── PLAN.md                   # Den här filen
├── README.md                 # Användarguide
├── pyproject.toml            # Beroenden + entrypoint
├── src/odoo_mcp/
│   ├── __init__.py
│   ├── server.py             # FastMCP-instans + tool-registreringar
│   ├── client.py             # XML-RPC-klient med caching
│   ├── instances.py          # prod/dev URL+credentials-läsning
│   ├── audit.py              # Postgres-audit-logger
│   ├── validators.py         # SE-bokföringsregler-checks
│   └── tools/
│       ├── read.py           # search/get-verktygen
│       ├── write_safe.py     # draft-skapare
│       └── write_critical.py # post + payment med confirm
├── tests/
│   ├── test_client.py
│   ├── test_validators.py
│   └── test_tools.py
└── docs/
    ├── ARCHITECTURE.md
    ├── DEPLOY.md             # Deploy-recept (systemd, Docker, supergateway)
    └── TOOLS.md              # Komplett tool-referens
```

## Beroenden

```toml
# pyproject.toml
[project]
name = "odoo-mcp"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=0.2.0",
    "psycopg2-binary>=2.9",   # för audit-logg
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]

[project.scripts]
odoo-mcp = "odoo_mcp.server:main"
```

## Konfiguration

Via miljövariabler (laddas från en secret-manager eller `.env`-fil):

```bash
ODOO_PROD_URL=https://odoo.example.com
ODOO_PROD_DB=odoo
ODOO_PROD_USER=user@example.com
ODOO_PROD_PASSWORD=<your-password>

ODOO_DEV_URL=https://odoo-dev.example.com
ODOO_DEV_DB=odoo
ODOO_DEV_USER=user@example.com
ODOO_DEV_PASSWORD=<your-password>

# Optional — audit log destination
MCP_AUDIT_DB_URL=postgresql://user:pass@host/dbname
```

## Open questions för senare

- **Multi-Claude-session-isolering:** ska dev-instans alltid vara åtkomlig
  utan confirm, eller alltid kräva confirm? (Just nu plan: ingen confirm
  på dev, confirm på prod för write_critical)
- **Rate-limiting:** behövs?
- **Dry-run-läge** för write-tools? Returnera summary av vad som hade hänt
  utan att faktiskt göra det.
- **Tool-output:** ska vi returnera structured data + human-readable
  summary, eller bara structured? Claude pratar bättre om det finns
  summary-text.
