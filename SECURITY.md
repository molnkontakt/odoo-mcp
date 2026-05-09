# Security Policy

## Supported Versions

Only the `main` branch is supported. The project is in active development —
breaking changes may land on `main` between tagged releases.

## Reporting a Vulnerability

If you discover a security vulnerability in odoo-mcp, please report it
**privately** via GitHub's [private vulnerability reporting](https://github.com/molnkontakt/odoo-mcp/security/advisories/new).

Please include:

- A description of the vulnerability
- Steps to reproduce
- Affected version / commit hash
- Any suggested mitigation

We aim to acknowledge reports within 5 business days and provide a status
update within 14 days.

## Scope

In scope:

- The `odoo-mcp` Python package
- Tool implementations under `src/odoo_mcp/tools/`
- The XML-RPC client (`src/odoo_mcp/client.py`)

Out of scope:

- Vulnerabilities in upstream Odoo (report to Odoo SA directly)
- Vulnerabilities in transport layer dependencies (report to upstream
  project, e.g. `fastmcp`)
- Issues that require a malicious actor to already control the host
  running the MCP server

## Security-relevant design notes

- Credentials are read from environment variables only — never hard-coded
  or read from files at rest in the repo
- Tool calls are split into `read`, `write_safe`, and `write_critical`
  tiers; write_critical requires explicit `confirm=True` in payload
- Audit logging is recommended for production deployments — see PLAN.md

## Hall of Fame

(Researchers who responsibly disclose will be acknowledged here.)
