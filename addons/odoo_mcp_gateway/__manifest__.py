{
    "name": "MCP gateway: act as the calling user",
    "version": "19.0.1.1.0",
    "category": "Technical",
    "summary": "RPC endpoint that runs a method as another user, for MCP servers that authenticate callers with OAuth",
    "description": """
Odoo does not accept OIDC tokens over XML-RPC, so an MCP server (github.com/molnkontakt/odoo-mcp)
logs in with one service account. This module lets that service account run calls *as* the
person behind the OAuth token: ``mcp.gateway.execute_as(login, model, method, args, kwargs)``
switches the environment to that user, so Odoo's own access rights and record rules decide what
the caller may see and do. Only members of the group *MCP gateway / Får byta användare* may call it,
and every call is logged with both identities.
    """,
    "author": "Molnkontakt AB",
    "license": "LGPL-3",
    "website": "https://github.com/molnkontakt/odoo-mcp",
    "depends": ["base"],
    "data": ["security/groups.xml"],
    "installable": True,
}
