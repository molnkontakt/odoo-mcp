"""End-to-end tests for the remote (HTTP + OAuth) transport.

The unit tests in `test_auth.py` inject a fake token. These run a real
uvicorn server with a real JWT verifier and a real MCP client, so they cover
the parts that only exist in a live request: the RFC 9728 metadata a web
client needs to find the IdP, the 401 challenge that points at it, and the
token → scopes → tool-guard chain through FastMCP's request context.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, RSAKeyPair

from odoo_mcp import auth
from odoo_mcp.auth import (
    SCOPE_CRITICAL,
    SCOPE_PROD,
    SCOPE_READ,
    SCOPE_WRITE,
    AuthSettings,
    current_identity,
    requires_scope,
)

ISSUER = "https://auth.test.example/application/o/odoo-mcp/"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def keypair():
    return RSAKeyPair.generate()


@pytest.fixture(scope="module")
def live_server(keypair):
    """A minimal odoo-mcp-shaped server: same auth provider, same guards."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    audience = f"{base_url}/mcp"

    provider = RemoteAuthProvider(
        token_verifier=JWTVerifier(
            public_key=keypair.public_key,
            issuer=ISSUER,
            audience=audience,
        ),
        authorization_servers=[ISSUER],
        base_url=base_url,
        scopes_supported=list(auth.ALL_SCOPES),
        resource_name="odoo-mcp",
    )
    server_mcp = FastMCP("odoo-mcp-test", auth=provider)

    @server_mcp.tool()
    @requires_scope(SCOPE_WRITE)
    def draft_something(instance: str) -> dict[str, Any]:
        identity = current_identity()
        return {
            "ran": True,
            "instance": instance,
            "actor": identity.actor,
            "actor_sub": identity.actor_sub,
            "scopes": list(identity.scopes),
        }

    config = uvicorn.Config(
        server_mcp.http_app(path="/mcp"),
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - only on a broken environment
        raise RuntimeError("test server did not start")

    yield base_url, audience

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _oauth_enabled(live_server):
    """The guards read process-wide settings; mirror the live server's."""
    base_url, audience = live_server
    auth.set_auth_settings(
        AuthSettings(
            mode="oauth",
            issuer=ISSUER,
            jwks_uri=f"{ISSUER}jwks/",
            audiences=(audience,),
            base_url=base_url,
        )
    )
    yield
    auth.set_auth_settings(None)


def mint(keypair, audience: str, scopes: list[str], **claims: Any) -> str:
    return keypair.create_token(
        subject=claims.pop("subject", "ak-uuid-1"),
        issuer=ISSUER,
        audience=audience,
        scopes=scopes,
        additional_claims={"email": "johan@example.com", **claims},
    )


def call_tool(url: str, token: str | None, **kwargs: Any) -> Any:
    async def run() -> Any:
        async with Client(url, auth=token) as client:
            return await client.call_tool("draft_something", kwargs)

    return asyncio.run(run())


# --------------------------------------------------------------------------
# Discovery: what a web client needs before it can log in
# --------------------------------------------------------------------------


class TestProtectedResourceMetadata:
    def test_metadata_points_at_the_authorization_server(self, live_server):
        base_url, audience = live_server
        doc = httpx.get(
            f"{base_url}/.well-known/oauth-protected-resource/mcp"
        ).json()
        assert doc["resource"] == audience
        assert doc["authorization_servers"] == [ISSUER]
        assert set(doc["scopes_supported"]) == set(auth.ALL_SCOPES)

    def test_unauthenticated_call_challenges_with_resource_metadata(self, live_server):
        """Without this header a claude.ai connector cannot start the flow."""
        base_url, _ = live_server
        resp = httpx.post(
            f"{base_url}/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert "resource_metadata=" in challenge
        assert "/.well-known/oauth-protected-resource/mcp" in challenge


# --------------------------------------------------------------------------
# Token validation
# --------------------------------------------------------------------------


class TestTokenValidation:
    def test_token_for_another_audience_is_rejected(self, live_server, keypair):
        base_url, _ = live_server
        token = mint(keypair, "https://some-other-service.example/mcp", [SCOPE_WRITE])
        with pytest.raises(httpx.HTTPStatusError, match="401"):
            call_tool(f"{base_url}/mcp", token, instance="dev")

    def test_token_from_another_issuer_is_rejected(self, live_server, keypair):
        base_url, audience = live_server
        token = keypair.create_token(
            issuer="https://evil.example/",
            audience=audience,
            scopes=[SCOPE_WRITE],
        )
        with pytest.raises(httpx.HTTPStatusError, match="401"):
            call_tool(f"{base_url}/mcp", token, instance="dev")


# --------------------------------------------------------------------------
# Scope enforcement over the wire
# --------------------------------------------------------------------------


class TestScopeEnforcement:
    def test_granted_scope_runs_the_tool(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_WRITE])
        result = call_tool(f"{base_url}/mcp", token, instance="dev")
        assert result.data["ran"] is True

    def test_missing_scope_is_denied(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_READ])
        with pytest.raises(ToolError) as exc:
            call_tool(f"{base_url}/mcp", token, instance="dev")
        assert SCOPE_WRITE in str(exc.value), "the client must learn which scope it needs"

    def test_higher_tier_implies_lower(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_CRITICAL])
        result = call_tool(f"{base_url}/mcp", token, instance="dev")
        assert result.data["ran"] is True

    def test_prod_requires_the_prod_scope(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_CRITICAL])
        with pytest.raises(ToolError, match=SCOPE_PROD):
            call_tool(f"{base_url}/mcp", token, instance="prod")

    def test_prod_allowed_with_the_prod_scope(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_CRITICAL, SCOPE_PROD])
        result = call_tool(f"{base_url}/mcp", token, instance="prod")
        assert result.data["instance"] == "prod"


# --------------------------------------------------------------------------
# Identity, which is what the audit log records
# --------------------------------------------------------------------------


class TestIdentityInRequestContext:
    def test_identity_comes_from_the_verified_token(self, live_server, keypair):
        base_url, audience = live_server
        token = mint(keypair, audience, [SCOPE_WRITE], subject="ak-uuid-42")
        result = call_tool(f"{base_url}/mcp", token, instance="dev")
        assert result.data["actor"] == "johan@example.com"
        assert result.data["actor_sub"] == "ak-uuid-42"
        assert SCOPE_READ in result.data["scopes"], "implied scopes are expanded"


# --------------------------------------------------------------------------
# Proxy mode: the DCR front that Authentik does not provide
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def proxy_server(keypair, module_monkeypatch):
    """`build_auth_provider` in oauth-proxy mode, against a stubbed IdP."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    audience = f"{base_url}/mcp"

    module_monkeypatch.setattr(
        auth,
        "discover_oidc",
        lambda issuer, **kw: {
            "jwks_uri": f"{ISSUER}jwks/",
            "authorization_endpoint": f"{ISSUER}authorize/",
            "token_endpoint": f"{ISSUER}token/",
        },
    )
    provider = auth.build_auth_provider(
        AuthSettings(
            mode="oauth-proxy",
            issuer=ISSUER,
            audiences=(audience,),
            base_url=base_url,
            client_id="odoo-mcp-client",
            client_secret="s3cret",
            allowed_client_redirect_uris=auth.DEFAULT_CLIENT_REDIRECT_URIS,
        )
    )
    server_mcp = FastMCP("odoo-mcp-proxy-test", auth=provider)

    @server_mcp.tool()
    @requires_scope(SCOPE_READ)
    def peek(instance: str) -> str:
        return "ok"

    config = uvicorn.Config(
        server_mcp.http_app(path="/mcp"), host="127.0.0.1", port=port, log_level="error"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover
        raise RuntimeError("proxy test server did not start")

    yield base_url

    server.should_exit = True
    thread.join(timeout=10)


class TestOAuthProxyMode:
    def test_advertises_dynamic_client_registration(self, proxy_server):
        """Claude clients self-register; Authentik has no registration_endpoint."""
        doc = httpx.get(
            f"{proxy_server}/.well-known/oauth-authorization-server"
        ).json()
        assert doc["registration_endpoint"] == f"{proxy_server}/register"
        assert doc["authorization_endpoint"] == f"{proxy_server}/authorize"
        assert doc["code_challenge_methods_supported"] == ["S256"]
        assert set(auth.ALL_SCOPES).issubset(set(doc["scopes_supported"]))

    def test_resource_metadata_points_at_this_server(self, proxy_server):
        doc = httpx.get(
            f"{proxy_server}/.well-known/oauth-protected-resource/mcp"
        ).json()
        assert doc["resource"] == f"{proxy_server}/mcp"
        assert doc["authorization_servers"] == [proxy_server.rstrip("/") + "/"]

    def test_still_rejects_unauthenticated_calls(self, proxy_server):
        resp = httpx.post(
            f"{proxy_server}/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert resp.status_code == 401
        assert "resource_metadata=" in resp.headers["www-authenticate"]

    def test_identity_scopes_are_requestable(self, proxy_server):
        """`email` is not a permission, but the audit log's actor depends on it."""
        doc = httpx.get(
            f"{proxy_server}/.well-known/oauth-authorization-server"
        ).json()
        assert set(auth.IDENTITY_SCOPES).issubset(set(doc["scopes_supported"]))
