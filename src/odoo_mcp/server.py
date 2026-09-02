"""CLI entrypoint for the odoo-mcp FastMCP server.

The actual `FastMCP` instance lives in `app.py` so tool modules can import it
without creating an import cycle through this module.

Two transports:

- `stdio` (default) — one server process per client, started by the client.
  The trust boundary is the local process; there is no OAuth.
- `http` — one long-lived server, many remote clients (claude.ai custom
  connectors, Claude Desktop, ChatGPT). Every request must carry a verified
  access token, so this transport refuses to start without OAuth unless the
  operator explicitly opts out for local testing.

Bind to localhost and terminate TLS in front of the process; the server speaks
plain HTTP and trusts nothing about the network it sits on.
"""

from __future__ import annotations

import os

from odoo_mcp.app import mcp
from odoo_mcp.auth import AuthConfigError, get_auth_settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_PATH = "/mcp"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def resolve_transport(env: dict[str, str] | None = None) -> str:
    src = os.environ if env is None else env
    transport = (src.get("MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("streamable-http", "streamable_http"):
        transport = "http"
    if transport not in ("stdio", "http"):
        raise AuthConfigError(
            f"MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}."
        )
    return transport


def check_http_is_authenticated(env: dict[str, str] | None = None) -> None:
    """Refuse to serve HTTP without token verification.

    An unauthenticated HTTP listener is an anonymous, internet-reachable
    accounting API — including the tools that post entries and register
    payments. The escape hatch exists for loopback testing and says so in its
    name.
    """
    src = os.environ if env is None else env
    if get_auth_settings().enabled:
        return
    if _truthy(src.get("MCP_ALLOW_UNAUTHENTICATED_HTTP")):
        return
    raise AuthConfigError(
        "Refusing to start the HTTP transport with MCP_AUTH_MODE=none: every "
        "caller would be anonymous, including for post/payment tools. Set "
        "MCP_AUTH_MODE=oauth (see docs/DEPLOY.md), or "
        "MCP_ALLOW_UNAUTHENTICATED_HTTP=1 for local loopback testing only."
    )


def main() -> None:
    """Run the MCP server over the configured transport."""
    # Tool modules register themselves on import via @mcp.tool()
    from odoo_mcp.tools import read, write_critical, write_safe  # noqa: F401

    transport = resolve_transport()
    if transport == "stdio":
        mcp.run()
        return

    check_http_is_authenticated()
    mcp.run(
        transport="http",
        host=os.environ.get("MCP_HTTP_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MCP_HTTP_PORT", DEFAULT_PORT)),
        path=os.environ.get("MCP_HTTP_PATH", DEFAULT_PATH),
    )


if __name__ == "__main__":
    main()
