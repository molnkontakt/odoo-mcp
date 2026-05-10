"""FastMCP app instance.

Lives in a separate module from `server.py` so tool modules can import it
without creating an import cycle (server.py imports tools, tools import
this).
"""

from fastmcp import FastMCP

mcp = FastMCP("odoo-mcp")
