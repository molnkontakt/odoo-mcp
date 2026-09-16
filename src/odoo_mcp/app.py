"""FastMCP app instance.

Lives in a separate module from `server.py` so tool modules can import it
without creating an import cycle (server.py imports tools, tools import
this).

The auth provider is attached here, at construction, because FastMCP binds it
to the HTTP app when the transport starts. With `MCP_AUTH_MODE=none` (the
default, and what stdio uses) `build_auth_provider()` returns None and the
server behaves exactly as it did before.
"""

from fastmcp import FastMCP

from odoo_mcp.auth import build_auth_provider

mcp = FastMCP("odoo-mcp", auth=build_auth_provider())
