"""Tests for the OAuth resource-server layer.

The point of these is the failure direction: a misconfigured or scope-less
caller must be *denied*, and the audit row must carry the identity the token
carried — not one the caller supplied.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from odoo_mcp import auth, server
from odoo_mcp.auth import (
    SCOPE_CRITICAL,
    SCOPE_PROD,
    SCOPE_READ,
    SCOPE_WRITE,
    AuthConfigError,
    AuthSettings,
    Identity,
    ScopeDenied,
)

PROXY_ENV_EXTRA = {
    "MCP_AUTH_MODE": "oauth-proxy",
    "MCP_OAUTH_CLIENT_ID": "odoo-mcp-client",
    "MCP_OAUTH_CLIENT_SECRET": "s3cret",
}

OAUTH_ENV = {
    "MCP_AUTH_MODE": "oauth",
    "MCP_OAUTH_ISSUER": "https://auth.example.com/application/o/odoo-mcp/",
    "MCP_OAUTH_AUDIENCE": "https://odoo-mcp.example.com/mcp",
    "MCP_PUBLIC_URL": "https://odoo-mcp.example.com",
    "MCP_OAUTH_JWKS_URI": "https://auth.example.com/application/o/odoo-mcp/jwks/",
}


PROXY_ENV = {**OAUTH_ENV, **PROXY_ENV_EXTRA}


class FakeToken:
    """Stand-in for fastmcp's AccessToken."""

    def __init__(self, scopes: list[str], claims: dict[str, Any] | None = None,
                 client_id: str = "claude-web"):
        self.scopes = scopes
        self.claims = claims if claims is not None else {"sub": "ak-uuid-1",
                                                         "email": "j@example.com"}
        self.client_id = client_id


@pytest.fixture(autouse=True)
def _reset_settings():
    auth.set_auth_settings(None)
    yield
    auth.set_auth_settings(None)


@pytest.fixture
def oauth_on(monkeypatch):
    """Turn OAuth on without needing an IdP."""
    auth.set_auth_settings(auth.load_auth_settings(OAUTH_ENV))

    def _login(token: FakeToken | None):
        monkeypatch.setattr(auth, "_current_token", lambda: token)

    return _login


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class TestLoadAuthSettings:
    def test_defaults_to_disabled(self):
        settings = auth.load_auth_settings({})
        assert settings.mode == "none"
        assert not settings.enabled

    def test_rejects_unknown_mode(self):
        with pytest.raises(AuthConfigError, match="MCP_AUTH_MODE"):
            auth.load_auth_settings({"MCP_AUTH_MODE": "maybe"})

    def test_oauth_requires_issuer_audience_and_public_url(self):
        with pytest.raises(AuthConfigError) as exc:
            auth.load_auth_settings({"MCP_AUTH_MODE": "oauth"})
        message = str(exc.value)
        for var in ("MCP_OAUTH_ISSUER", "MCP_OAUTH_AUDIENCE", "MCP_PUBLIC_URL"):
            assert var in message, "the error must name every missing variable"

    def test_oauth_settings_are_read(self):
        settings = auth.load_auth_settings(OAUTH_ENV)
        assert settings.enabled
        assert settings.issuer == OAUTH_ENV["MCP_OAUTH_ISSUER"]
        assert settings.audiences == (OAUTH_ENV["MCP_OAUTH_AUDIENCE"],)
        assert settings.audience == OAUTH_ENV["MCP_OAUTH_AUDIENCE"]
        assert settings.jwks_uri == OAUTH_ENV["MCP_OAUTH_JWKS_URI"]

    def test_jwks_falls_back_to_discovery(self, monkeypatch):
        env = {k: v for k, v in OAUTH_ENV.items() if k != "MCP_OAUTH_JWKS_URI"}
        settings = auth.load_auth_settings(env)
        assert settings.jwks_uri is None

        called: list[str] = []
        monkeypatch.setattr(
            auth, "discover_oidc",
            lambda issuer, **kw: called.append(issuer)
            or {"jwks_uri": "https://idp/jwks"},
        )
        monkeypatch.setattr(
            "fastmcp.server.auth.providers.jwt.JWTVerifier.__init__",
            lambda self, **kw: None,
        )
        monkeypatch.setattr(
            "fastmcp.server.auth.RemoteAuthProvider.__init__",
            lambda self, **kw: None,
        )
        auth.build_auth_provider(settings)
        assert called == [settings.issuer]

    def test_discovery_failure_is_fatal(self, monkeypatch):
        def boom(url, timeout=0):
            raise OSError("connection refused")

        monkeypatch.setattr(auth.urllib.request, "urlopen", boom)
        with pytest.raises(AuthConfigError, match="OIDC discovery failed"):
            auth.discover_jwks_uri("https://auth.example.com/application/o/x/")

    def test_no_provider_when_disabled(self):
        assert auth.build_auth_provider(AuthSettings()) is None

    def test_audience_may_list_several_values(self):
        env = dict(OAUTH_ENV, MCP_OAUTH_AUDIENCE="https://a/mcp, client-id-123")
        settings = auth.load_auth_settings(env)
        assert settings.audiences == ("https://a/mcp", "client-id-123")
        assert settings.audience == ["https://a/mcp", "client-id-123"]


class TestProxyModeSettings:
    """Proxy mode exists because Authentik has no Dynamic Client Registration."""

    def test_proxy_requires_upstream_client_credentials(self):
        with pytest.raises(AuthConfigError) as exc:
            auth.load_auth_settings(dict(OAUTH_ENV, MCP_AUTH_MODE="oauth-proxy"))
        message = str(exc.value)
        assert "MCP_OAUTH_CLIENT_ID" in message
        assert "MCP_OAUTH_CLIENT_SECRET" in message

    def test_proxy_settings_are_read(self):
        settings = auth.load_auth_settings(PROXY_ENV)
        assert settings.mode == "oauth-proxy"
        assert settings.enabled
        assert settings.client_id == "odoo-mcp-client"
        assert settings.client_secret == "s3cret"

    def test_client_redirects_default_to_an_allow_list(self):
        """None would mean 'any redirect URI', i.e. an open redirector."""
        settings = auth.load_auth_settings(PROXY_ENV)
        assert settings.allowed_client_redirect_uris == (
            auth.DEFAULT_CLIENT_REDIRECT_URIS
        )
        assert "https://claude.ai/api/mcp/auth_callback" in (
            settings.allowed_client_redirect_uris
        )

    def test_client_redirects_can_be_overridden(self):
        settings = auth.load_auth_settings(
            dict(PROXY_ENV, MCP_OAUTH_CLIENT_REDIRECT_URIS="http://localhost:*, https://x/cb")
        )
        assert settings.allowed_client_redirect_uris == (
            "http://localhost:*",
            "https://x/cb",
        )

    def test_plain_oauth_needs_no_client_credentials(self):
        settings = auth.load_auth_settings(OAUTH_ENV)
        assert settings.client_id is None


# --------------------------------------------------------------------------
# Scope model
# --------------------------------------------------------------------------


class TestExpandScopes:
    def test_critical_implies_write_and_read(self):
        assert auth.expand_scopes([SCOPE_CRITICAL]) == {
            SCOPE_CRITICAL, SCOPE_WRITE, SCOPE_READ,
        }

    def test_read_implies_nothing_further(self):
        assert auth.expand_scopes([SCOPE_READ]) == {SCOPE_READ}

    def test_prod_is_orthogonal_to_the_tier_chain(self):
        assert auth.expand_scopes([SCOPE_PROD]) == {SCOPE_PROD}
        assert SCOPE_PROD not in auth.expand_scopes([SCOPE_CRITICAL])

    def test_garbage_scopes_claim_yields_nothing(self):
        assert auth.expand_scopes("odoo:critical") == frozenset()
        assert auth.expand_scopes(None) == frozenset()


class TestCheckScopes:
    def test_noop_when_auth_disabled(self):
        auth.set_auth_settings(AuthSettings())
        auth.check_scopes(SCOPE_CRITICAL)  # stdio: no token exists at all

    def test_denies_when_no_token(self, oauth_on):
        oauth_on(None)
        with pytest.raises(ScopeDenied, match="no verified access token"):
            auth.check_scopes(SCOPE_READ)

    def test_allows_granted_scope(self, oauth_on):
        oauth_on(FakeToken([SCOPE_READ]))
        auth.check_scopes(SCOPE_READ)

    def test_denies_missing_scope_and_names_it(self, oauth_on):
        oauth_on(FakeToken([SCOPE_READ]))
        with pytest.raises(ScopeDenied, match=SCOPE_CRITICAL):
            auth.check_scopes(SCOPE_CRITICAL)

    def test_implied_scope_is_enough(self, oauth_on):
        oauth_on(FakeToken([SCOPE_CRITICAL]))
        auth.check_scopes(SCOPE_READ)


class TestCheckInstance:
    def test_dev_needs_no_prod_scope(self, oauth_on):
        oauth_on(FakeToken([SCOPE_WRITE]))
        auth.check_instance("dev")

    def test_prod_requires_prod_scope(self, oauth_on):
        oauth_on(FakeToken([SCOPE_CRITICAL]))
        with pytest.raises(ScopeDenied, match=SCOPE_PROD):
            auth.check_instance("prod")

    def test_prod_allowed_with_prod_scope(self, oauth_on):
        oauth_on(FakeToken([SCOPE_WRITE, SCOPE_PROD]))
        auth.check_instance("prod")

    def test_prod_gate_is_case_insensitive(self, oauth_on):
        oauth_on(FakeToken([SCOPE_CRITICAL]))
        with pytest.raises(ScopeDenied):
            auth.check_instance("PROD")


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


class TestCurrentIdentity:
    def test_local_when_disabled(self):
        auth.set_auth_settings(AuthSettings())
        assert auth.current_identity() == auth.LOCAL_IDENTITY
        assert not auth.current_identity().authenticated

    def test_reads_email_sub_and_client(self, oauth_on):
        oauth_on(FakeToken([SCOPE_WRITE], client_id="claude-desktop"))
        ident = auth.current_identity()
        assert ident.actor == "j@example.com"
        assert ident.actor_sub == "ak-uuid-1"
        assert ident.client_id == "claude-desktop"
        assert ident.authenticated

    def test_falls_back_to_sub_when_no_email(self, oauth_on):
        oauth_on(FakeToken([SCOPE_READ], claims={"sub": "ak-uuid-2"}))
        assert auth.current_identity().actor == "ak-uuid-2"

    def test_unauthenticated_identity_when_token_missing(self, oauth_on):
        oauth_on(None)
        assert auth.current_identity() == Identity()


# --------------------------------------------------------------------------
# The decorator applied to every tool
# --------------------------------------------------------------------------


class TestRequiresScope:
    def test_body_does_not_run_when_scope_missing(self, oauth_on):
        oauth_on(FakeToken([SCOPE_READ]))
        ran: list[int] = []

        @auth.requires_scope(SCOPE_WRITE)
        def tool(instance: str) -> str:
            ran.append(1)
            return "done"

        with pytest.raises(ScopeDenied):
            tool("dev")
        assert ran == []

    def test_prod_gate_applies_to_the_instance_argument(self, oauth_on):
        oauth_on(FakeToken([SCOPE_READ]))

        @auth.requires_scope(SCOPE_READ)
        def tool(instance: str) -> str:
            return "done"

        assert tool("dev") == "done"
        assert tool(instance="dev") == "done"
        with pytest.raises(ScopeDenied, match=SCOPE_PROD):
            tool("prod")

    def test_signature_survives_wrapping(self):
        """FastMCP builds the tool schema from the signature."""

        @auth.requires_scope(SCOPE_READ)
        def tool(instance: str, limit: int = 20) -> str:
            return "done"

        params = inspect.signature(tool).parameters
        assert list(params) == ["instance", "limit"]
        assert params["limit"].default == 20


class TestToolsAreGuarded:
    """Scope checks that exist but are never called are not protection."""

    @pytest.mark.parametrize(
        "module_name, tool_name",
        [
            ("read", "odoo_search_partners"),
            ("read", "odoo_search_read"),
            ("write_safe", "odoo_create_journal_entry_draft"),
            ("write_safe", "odoo_create_invoice"),
            ("write_critical", "odoo_post_journal_entry"),
            ("write_critical", "odoo_register_payment"),
            ("write_critical", "odoo_reverse_move"),
        ],
    )
    def test_tool_denies_a_token_with_no_scopes(self, oauth_on, module_name, tool_name):
        import importlib

        module = importlib.import_module(f"odoo_mcp.tools.{module_name}")
        tool = getattr(module, tool_name)
        oauth_on(FakeToken([]))
        with pytest.raises(ScopeDenied):
            tool(instance="dev")

    def test_every_registered_tool_is_wrapped(self):
        from odoo_mcp.tools import read, write_critical, write_safe

        for module in (read, write_safe, write_critical):
            names = [n for n in dir(module) if n.startswith("odoo_")]
            assert names
            for name in names:
                fn = getattr(module, name)
                assert getattr(fn, "__wrapped__", None) is not None, (
                    f"{module.__name__}.{name} is not scope-guarded"
                )


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


class TestTransportGuard:
    def test_http_refused_without_auth(self):
        auth.set_auth_settings(AuthSettings())
        with pytest.raises(AuthConfigError, match="MCP_AUTH_MODE=none"):
            server.check_http_is_authenticated({})

    def test_http_allowed_with_explicit_local_opt_out(self):
        auth.set_auth_settings(AuthSettings())
        server.check_http_is_authenticated({"MCP_ALLOW_UNAUTHENTICATED_HTTP": "1"})

    def test_http_allowed_with_oauth(self):
        auth.set_auth_settings(auth.load_auth_settings(OAUTH_ENV))
        server.check_http_is_authenticated({})

    def test_stdio_is_the_default(self):
        assert server.resolve_transport({}) == "stdio"

    def test_streamable_http_alias(self):
        assert server.resolve_transport({"MCP_TRANSPORT": "streamable-http"}) == "http"

    def test_unknown_transport_is_fatal(self):
        with pytest.raises(AuthConfigError, match="MCP_TRANSPORT"):
            server.resolve_transport({"MCP_TRANSPORT": "sse"})


# --------------------------------------------------------------------------
# Identity reaches the audit log
# --------------------------------------------------------------------------


class TestAuditCarriesCallerIdentity:
    """Odoo sees the shared service account, so this row is the only record."""

    @pytest.fixture
    def claims_seen(self, monkeypatch):
        from odoo_mcp import audit

        seen: list[dict[str, Any]] = []

        class FakeLogger:
            def claim(self, **kwargs):
                seen.append(kwargs)
                return 1

            def complete(self, *a, **kw):
                pass

        monkeypatch.setattr(audit, "get_logger", lambda: FakeLogger())
        return seen

    def test_actor_comes_from_the_token(self, oauth_on, claims_seen):
        from odoo_mcp.audit import audit_call

        oauth_on(FakeToken([SCOPE_CRITICAL], client_id="claude-web"))
        with audit_call(tool="odoo_post_journal_entry", instance="dev", params={}):
            pass

        row = claims_seen[0]
        assert row["actor"] == "j@example.com"
        assert row["actor_sub"] == "ak-uuid-1"
        assert row["client_id"] == "claude-web"

    def test_stdio_rows_are_labelled_local(self, claims_seen):
        from odoo_mcp.audit import audit_call

        auth.set_auth_settings(AuthSettings())
        with audit_call(tool="odoo_post_journal_entry", instance="dev", params={}):
            pass

        row = claims_seen[0]
        assert row["actor"] == "local:stdio"
        assert row["actor_sub"] is None


class TestDiscoveryUserAgent:
    """Cloudflare's managed rules 403 urllib's default User-Agent."""

    def test_discovery_sends_an_explicit_user_agent(self, monkeypatch):
        seen: dict[str, Any] = {}

        class FakeResponse:
            def read(self):
                return b'{"jwks_uri": "https://idp/jwks"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            seen["ua"] = request.get_header("User-agent")
            return FakeResponse()

        monkeypatch.setattr(auth.urllib.request, "urlopen", fake_urlopen)
        auth.discover_oidc("https://auth.example.com/application/o/x/")
        assert seen["ua"] == auth.USER_AGENT
        assert "python-urllib" not in seen["ua"].lower()
