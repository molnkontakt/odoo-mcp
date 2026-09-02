"""OAuth 2.1 resource-server auth for the remote (HTTP) transport.

Two auth boundaries, and only the first one is OAuth
-----------------------------------------------------
OAuth here gates **client → this server**. The **server → Odoo** hop is a
separate boundary and always needs an Odoo credential: `auth_oidc` in Odoo is
web-login only, so an Authentik access token is not accepted by XML-RPC. This
server therefore holds *one* minimally scoped Odoo service account
(`instances.py`) and never forwards the caller's token to Odoo.

That makes Odoo's own `create_uid`/`write_uid` show the service account for
every action, so caller attribution lives **only** in `mcp_audit`
(`actor`, `actor_sub`, `client_id`). Anything that weakens the audit log
weakens the only record of who did what — see `audit.py`.

Per-user Odoo API keys were considered and rejected: one server holding every
accountant's Odoo key is a confused deputy that makes Odoo-side attribution
look real while the server can still act as anyone.

What this module gives the client
---------------------------------
`build_auth_provider()` returns a FastMCP `RemoteAuthProvider`, which serves
RFC 9728 Protected Resource Metadata at
`/.well-known/oauth-protected-resource` and answers unauthenticated calls with
a `WWW-Authenticate` challenge carrying `resource_metadata`. That pair is what
lets a web client (claude.ai custom connectors, Claude Desktop) discover the
authorization server on its own. A sibling server in this homelab shipped
without them and no web connector could complete a flow.

Token validation is delegated to a real IdP (Authentik) over JWKS — this
server is a *resource server* only. It deliberately does not implement an
authorization server, and in particular no Dynamic Client Registration: the
sibling server's DCR endpoint auto-approved every client with no login and no
consent.

Scopes are enforced at call sites
---------------------------------
`@requires_scope(...)` wraps every tool function, so a scope that is not
granted denies the call rather than decorating the metadata. Scope checks that
exist but are never called are worse than none, because they read as
protection.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

#: Read tier: every tool in `tools/read.py`.
SCOPE_READ = "odoo:read"
#: Draft-creating tier: every tool in `tools/write_safe.py`.
SCOPE_WRITE = "odoo:write"
#: Ledger-moving tier: post / payment / reversal in `tools/write_critical.py`.
SCOPE_CRITICAL = "odoo:critical"
#: Required *in addition* to the tier scope to touch the production instance.
SCOPE_PROD = "odoo:prod"

ALL_SCOPES: tuple[str, ...] = (SCOPE_READ, SCOPE_WRITE, SCOPE_CRITICAL, SCOPE_PROD)

#: Sent on the discovery request. See `discover_oidc`.
USER_AGENT = "odoo-mcp/0.1 (+https://github.com/molnkontakt/odoo-mcp)"

#: Tier scopes imply the tiers below them: a caller allowed to post a journal
#: entry can obviously read it. `odoo:prod` is deliberately outside this chain —
#: it is an orthogonal axis (which ledger), not a stronger tier.
_IMPLIES: dict[str, tuple[str, ...]] = {
    SCOPE_CRITICAL: (SCOPE_WRITE,),
    SCOPE_WRITE: (SCOPE_READ,),
}

AuthMode = Literal["none", "oauth", "oauth-proxy"]

F = TypeVar("F", bound=Callable[..., Any])


class AuthConfigError(RuntimeError):
    """Raised at startup when the auth configuration is unusable.

    Always fatal: a resource server that starts with a half-configured
    verifier is a resource server that accepts unverified tokens.
    """


class ScopeDenied(Exception):
    """Raised when the caller's token lacks a scope the tool requires."""


@dataclass(frozen=True)
class AuthSettings:
    mode: AuthMode = "none"
    issuer: str | None = None
    jwks_uri: str | None = None
    #: Accepted `aud` values. More than one is allowed because Authentik's
    #: audience override lives in a scope mapping, and a token minted before
    #: that mapping applied carries `aud = client_id` instead. Keep the list
    #: short: every entry is a token this server will accept.
    audiences: tuple[str, ...] = ()
    base_url: str | None = None
    resource_name: str = "odoo-mcp"
    #: Upstream (IdP) client credentials — proxy mode only.
    client_id: str | None = None
    client_secret: str | None = None
    allowed_client_redirect_uris: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    @property
    def audience(self) -> str | list[str] | None:
        """Shape the verifier expects: a bare string when there is only one."""
        if not self.audiences:
            return None
        return self.audiences[0] if len(self.audiences) == 1 else list(self.audiences)


@dataclass(frozen=True)
class Identity:
    """Who is calling, as far as this server can tell.

    `actor` is for humans reading the audit log; `actor_sub` is the stable
    identifier to correlate on, because emails get renamed.
    """

    actor: str | None = None
    actor_sub: str | None = None
    client_id: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def authenticated(self) -> bool:
        return self.actor_sub is not None


#: Identity used when the server runs without OAuth (stdio, local dev). Named,
#: not None, so an audit row can never be mistaken for one from a real caller.
LOCAL_IDENTITY = Identity(actor="local:stdio")


#: Redirect URIs accepted from MCP clients in proxy mode. Wildcards allowed.
#: `None` (the FastMCP default) accepts *any* client redirect, which turns the
#: DCR endpoint into an open redirector for the authorization code.
DEFAULT_CLIENT_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
)


def load_auth_settings(env: dict[str, str] | None = None) -> AuthSettings:
    """Read auth configuration from the environment.

    Pure apart from the OIDC discovery call in `build_auth_provider`, so it can
    be tested without an IdP.
    """
    src = os.environ if env is None else env
    mode = (src.get("MCP_AUTH_MODE") or "none").strip().lower()
    if mode not in ("none", "oauth", "oauth-proxy"):
        raise AuthConfigError(
            f"MCP_AUTH_MODE must be 'none', 'oauth' or 'oauth-proxy', "
            f"got {mode!r}."
        )
    if mode == "none":
        return AuthSettings(mode="none")

    issuer = (src.get("MCP_OAUTH_ISSUER") or "").strip()
    audience = (src.get("MCP_OAUTH_AUDIENCE") or "").strip()
    base_url = (src.get("MCP_PUBLIC_URL") or "").strip()
    jwks_uri = (src.get("MCP_OAUTH_JWKS_URI") or "").strip()
    client_id = (src.get("MCP_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (src.get("MCP_OAUTH_CLIENT_SECRET") or "").strip()
    redirects = (src.get("MCP_OAUTH_CLIENT_REDIRECT_URIS") or "").strip()

    required: list[tuple[str, str]] = [
        ("MCP_OAUTH_ISSUER", issuer),
        ("MCP_OAUTH_AUDIENCE", audience),
        ("MCP_PUBLIC_URL", base_url),
    ]
    if mode == "oauth-proxy":
        # The proxy is the IdP's client, so it needs the IdP's credentials.
        required += [
            ("MCP_OAUTH_CLIENT_ID", client_id),
            ("MCP_OAUTH_CLIENT_SECRET", client_secret),
        ]

    missing = [name for name, value in required if not value]
    if missing:
        raise AuthConfigError(
            f"MCP_AUTH_MODE={mode} requires {', '.join(missing)}. "
            f"MCP_OAUTH_ISSUER is the IdP issuer (Authentik: "
            f"https://auth.example.com/application/o/<slug>/), "
            f"MCP_OAUTH_AUDIENCE is the value this server requires in the "
            f"token's `aud` (comma-separate to accept more than one), "
            f"MCP_PUBLIC_URL is the externally reachable base URL of this "
            f"server, and MCP_OAUTH_CLIENT_ID/SECRET are the credentials the "
            f"IdP issued for *this server* as an OAuth client."
        )

    return AuthSettings(
        mode=mode,  # type: ignore[arg-type]
        issuer=issuer,
        jwks_uri=jwks_uri or None,
        audiences=tuple(a.strip() for a in audience.split(",") if a.strip()),
        base_url=base_url,
        resource_name=(src.get("MCP_RESOURCE_NAME") or "odoo-mcp").strip(),
        client_id=client_id or None,
        client_secret=client_secret or None,
        allowed_client_redirect_uris=(
            tuple(u.strip() for u in redirects.split(",") if u.strip())
            if redirects
            else DEFAULT_CLIENT_REDIRECT_URIS
        ),
    )


def discover_oidc(issuer: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch the issuer's OIDC discovery document.

    Failing here is fatal on purpose — starting without a key source means
    starting with no way to verify a token, and in proxy mode without the
    upstream endpoints there is nothing to proxy to.
    """
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    # An explicit User-Agent, because urllib's default (`Python-urllib/3.x`) is
    # blocked outright by Cloudflare's managed bot rules — a 403 that reads
    # like a dead endpoint or a firewall problem. Verified against
    # auth.molnkontakt.se 2026-09-02: urllib's default 403s, everything else
    # (including httpx's default, which fastmcp uses for JWKS) gets 200.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return dict(json.loads(resp.read().decode()))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AuthConfigError(
            f"OIDC discovery failed for issuer {issuer!r} ({url}): {exc}."
        ) from exc


def discover_jwks_uri(issuer: str, *, timeout: float = 10.0) -> str:
    """Resolve the issuer's JWKS URI. Used when `MCP_OAUTH_JWKS_URI` is unset."""
    doc = discover_oidc(issuer, timeout=timeout)
    jwks_uri = doc.get("jwks_uri")
    if not jwks_uri:
        raise AuthConfigError(
            f"OIDC discovery document for {issuer!r} has no `jwks_uri`. "
            f"Set MCP_OAUTH_JWKS_URI explicitly."
        )
    return str(jwks_uri)


_settings: AuthSettings | None = None


def get_auth_settings() -> AuthSettings:
    """Process-wide auth settings, resolved once at first use."""
    global _settings
    if _settings is None:
        _settings = load_auth_settings()
    return _settings


def set_auth_settings(settings: AuthSettings | None) -> None:
    """Override the cached settings. For tests and for `server.py` startup."""
    global _settings
    _settings = settings


def build_auth_provider(settings: AuthSettings | None = None) -> Any | None:
    """Build the FastMCP auth provider, or None when auth is disabled.

    Two shapes, because MCP clients differ in how they get a token:

    - ``oauth`` — plain resource server. The client already holds a token for
      this resource (it was pre-registered with the IdP, or it is a service).
    - ``oauth-proxy`` — resource server **plus** a thin authorization-server
      front. MCP clients (Claude Code, Claude Desktop, claude.ai connectors)
      expect RFC 7591 Dynamic Client Registration, and Authentik does not offer
      it outside its enterprise tier. The proxy accepts the client's DCR call,
      then runs the real flow upstream with *this server's* pre-registered
      credentials. The token the client ends up holding is still an
      Authentik-signed JWT, verified here exactly as in ``oauth`` mode.

    Proxy mode keeps two protections that a naive DCR endpoint drops: the
    upstream login/consent is Authentik's (so the IdP still decides who may
    log in), and client redirect URIs are matched against an allow-list rather
    than accepted as given.
    """
    settings = settings or get_auth_settings()
    if not settings.enabled:
        return None

    # Imported lazily: stdio deployments should not pay for authlib/JWKS
    # imports, and `app.py` is imported by every test.
    from fastmcp.server.auth import RemoteAuthProvider
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    assert settings.issuer and settings.audiences and settings.base_url

    doc: dict[str, Any] | None = None
    jwks_uri = settings.jwks_uri
    if jwks_uri is None or settings.mode == "oauth-proxy":
        doc = discover_oidc(settings.issuer)
        jwks_uri = jwks_uri or str(doc.get("jwks_uri") or "")
    if not jwks_uri:
        raise AuthConfigError(
            f"No JWKS URI for issuer {settings.issuer!r}; set MCP_OAUTH_JWKS_URI."
        )

    verifier = JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=settings.issuer,
        audience=settings.audience,
    )

    if settings.mode == "oauth":
        return RemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[settings.issuer],  # type: ignore[list-item]
            base_url=settings.base_url,
            scopes_supported=list(ALL_SCOPES),
            resource_name=settings.resource_name,
        )

    from fastmcp.server.auth.oauth_proxy import OAuthProxy

    assert doc is not None
    missing = [
        key
        for key in ("authorization_endpoint", "token_endpoint")
        if not doc.get(key)
    ]
    if missing:
        raise AuthConfigError(
            f"OIDC discovery document for {settings.issuer!r} is missing "
            f"{', '.join(missing)}; cannot proxy to it."
        )

    return OAuthProxy(
        upstream_authorization_endpoint=str(doc["authorization_endpoint"]),
        upstream_token_endpoint=str(doc["token_endpoint"]),
        upstream_revocation_endpoint=(
            str(doc["revocation_endpoint"]) if doc.get("revocation_endpoint") else None
        ),
        upstream_client_id=str(settings.client_id),
        upstream_client_secret=str(settings.client_secret),
        token_verifier=verifier,
        base_url=settings.base_url,
        valid_scopes=list(ALL_SCOPES),
        allowed_client_redirect_uris=list(settings.allowed_client_redirect_uris),
        # Every login goes through Authentik's own consent screen, and this
        # adds the "which client is asking" step on top. A DCR endpoint that
        # approves silently is how an unknown client gets a token.
        require_authorization_consent=True,
    )


def expand_scopes(scopes: object) -> frozenset[str]:
    """Expand granted scopes through the tier implications."""
    if not isinstance(scopes, (list, tuple, set, frozenset)):
        return frozenset()
    granted = {str(s) for s in scopes}
    changed = True
    while changed:
        changed = False
        for scope in list(granted):
            for implied in _IMPLIES.get(scope, ()):
                if implied not in granted:
                    granted.add(implied)
                    changed = True
    return frozenset(granted)


def _current_token() -> Any | None:
    from fastmcp.server.dependencies import get_access_token

    try:
        return get_access_token()
    except Exception:
        # No request in flight (direct call, stdio, tests).
        return None


def current_identity() -> Identity:
    """Resolve the caller from the access token of the request in flight."""
    if not get_auth_settings().enabled:
        return LOCAL_IDENTITY

    token = _current_token()
    if token is None:
        return Identity()

    claims: dict[str, Any] = getattr(token, "claims", None) or {}
    sub = claims.get("sub")
    actor = (
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("name")
        or sub
    )
    return Identity(
        actor=str(actor) if actor else None,
        actor_sub=str(sub) if sub else None,
        client_id=getattr(token, "client_id", None) or claims.get("azp"),
        scopes=tuple(sorted(expand_scopes(getattr(token, "scopes", ())))),
    )


def check_scopes(*needed: str, identity: Identity | None = None) -> None:
    """Raise ScopeDenied unless the caller holds every scope in `needed`.

    A no-op when auth is disabled: stdio deployments have no token, and the
    trust boundary there is the local process, not a scope string.
    """
    if not get_auth_settings().enabled:
        return

    ident = current_identity() if identity is None else identity
    if not ident.authenticated:
        raise ScopeDenied(
            "This tool requires an authenticated caller, but the request "
            "carried no verified access token."
        )

    missing = sorted(set(needed) - set(ident.scopes))
    if missing:
        raise ScopeDenied(
            f"Missing scope(s) {missing} for this tool. Granted: "
            f"{sorted(ident.scopes) or '[]'}. Request the scope from the "
            f"authorization server, or use a lower-tier tool."
        )


def check_instance(instance: str, *, identity: Identity | None = None) -> None:
    """Gate the production ledger behind `odoo:prod`.

    The tier scopes say *what kind* of call is allowed; this says *which
    ledger*. Keeping them separate is what lets a client be trusted to post
    entries in dev without being trusted to post them in prod.
    """
    if not get_auth_settings().enabled:
        return
    if str(instance).strip().lower() != "prod":
        return

    ident = current_identity() if identity is None else identity
    if SCOPE_PROD not in ident.scopes:
        raise ScopeDenied(
            f"Missing scope {SCOPE_PROD!r}: this token may only act on "
            f"non-production instances."
        )


def requires_scope(*needed: str) -> Callable[[F], F]:
    """Enforce scopes (and the prod gate) before a tool body runs.

    Applied *under* `@mcp.tool()` so the check is part of the function
    itself — the same guard then applies when the function is called directly,
    which is how the tests reach it.
    """

    def decorate(fn: F) -> F:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            check_scopes(*needed)
            bound = sig.bind_partial(*args, **kwargs)
            instance = bound.arguments.get("instance")
            if instance is not None:
                check_instance(str(instance))
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate
