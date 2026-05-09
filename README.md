# odoo-mcp

MCP-server för Odoo. Ger Claude (eller andra MCP-klienter) kontrollerad åtkomst
till en Odoo-instans via XML-RPC. Stöd för flera environments (typiskt prod + dev).

**Status:** Planeringsfasen — se [PLAN.md](PLAN.md).

## Snabbstart (under utveckling)

```bash
git clone https://github.com/<your-user>/odoo-mcp.git
cd odoo-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Sätt env-variabler för credentials
export ODOO_DEV_URL=https://odoo-dev.example.com
export ODOO_DEV_DB=odoo
export ODOO_DEV_USER=user@example.com
export ODOO_DEV_PASSWORD=...

# Kör lokalt (stdio)
odoo-mcp
```

## Anropa via Claude Code

```bash
claude mcp add odoo --transport stdio --command odoo-mcp
```

Eller HTTP via en gateway/proxy när deployad.

## Verktyg

Se [docs/TOOLS.md](docs/TOOLS.md) för komplett referens.

Tre kategorier:
- **read**: gratis, ingen confirm
- **write_safe**: skapar utkast, ingen confirm men loggat
- **write_critical**: kräver `confirm=True`, validerat mot SE-bokföringsregler

## Beroenden

- Python 3.11+
- FastMCP
- Odoo 16+ med XML-RPC aktiverat (default på alla self-hosted-installationer)
- (Optional) Phase eller annan secret-manager för credentials

## License

LGPL-3
