# odoo_mcp_gateway

Companion Odoo addon for [odoo-mcp](https://github.com/molnkontakt/odoo-mcp). Odoo does not
accept OIDC tokens over XML-RPC, so the MCP server logs in with one service account. With this
addon installed and `ODOO_<NAME>_IMPERSONATE=1` on the server, every tool call is executed
through `mcp.gateway.execute_as(login, model, method, args, kwargs)` as the Odoo user whose
`login` (or `oauth_uid`) equals the e-mail in the caller's OAuth token. Odoo's access rights
and record rules then apply to the caller, not to the service account.

- Only members of the group **MCP gateway / Får byta användare** may call `execute_as`. Give the
  service account that group and nothing else: it then cannot read or write anything on its own.
- Calls to unknown users are refused (the server never falls back to the service account).
- Every call is logged with both identities in Odoo's log; the MCP audit log keeps the token
  identity as well.
- Install like any addon (`/opt/oca/custom/odoo_mcp_gateway`, `-i odoo_mcp_gateway`).
