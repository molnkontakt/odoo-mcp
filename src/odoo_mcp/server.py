"""FastMCP server entry point.

Tools are registered from `odoo_mcp.tools.*` modules.
"""

from fastmcp import FastMCP

mcp = FastMCP("odoo-mcp")


def main() -> None:
    """CLI entrypoint — runs the MCP server over stdio."""
    # Tool modules register themselves on import via @mcp.tool()
    from odoo_mcp.tools import read  # noqa: F401
    # from odoo_mcp.tools import write_safe  # noqa: F401  (Phase 2)
    # from odoo_mcp.tools import write_critical  # noqa: F401  (Phase 3)
    mcp.run()


if __name__ == "__main__":
    main()
