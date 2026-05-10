"""CLI entrypoint for the odoo-mcp FastMCP server.

The actual `FastMCP` instance lives in `app.py` so tool modules can import
it without creating an import cycle through this module.
"""

from odoo_mcp.app import mcp


def main() -> None:
    """Run the MCP server over stdio."""
    # Tool modules register themselves on import via @mcp.tool()
    from odoo_mcp.tools import read, write_critical, write_safe  # noqa: F401
    mcp.run()


if __name__ == "__main__":
    main()
