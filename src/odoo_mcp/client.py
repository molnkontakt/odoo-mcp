"""XML-RPC client for Odoo with per-instance, per-thread connection caching.

Wraps xmlrpc.client.ServerProxy to:
- Authenticate once and cache the uid
- Provide a clean execute_kw interface
- Surface meaningful errors

Each instance ("prod" / "dev") gets its own cached client.

Thread safety
-------------
`ServerProxy` is NOT thread-safe: it holds a single `Transport`, which caches a
single `HTTPConnection` for keep-alive. Two threads sharing one proxy interleave
on that socket, and `http.client` raises `CannotSendRequest` on the loser. Worse,
`Transport.single_request` closes the shared connection on any exception, which
can tear the socket out from under a thread that has already put a *write* on the
wire — so a mutation may be applied in Odoo while the tool reports failure.

FastMCP runs sync tool functions in a worker thread pool, so concurrent calls are
normal, not exceptional. Each thread therefore gets its own pair of proxies via
`threading.local()`. Proxies are cheap; the connection underneath them is what
must not be shared.
"""

from __future__ import annotations

import contextvars
import http.client
import threading
import xmlrpc.client
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from odoo_mcp.instances import Instance, get_config, resolve_instance

#: Socket timeout (seconds) for every XML-RPC call. Without this a hung Odoo
#: blocks the calling worker thread forever, and the MCP server slowly loses its
#: thread pool with no error anywhere.
DEFAULT_TIMEOUT = 60


class _TimeoutTransport(xmlrpc.client.Transport):
    """`Transport` that applies a socket timeout to its connection."""

    def __init__(self, *args: Any, timeout: float = DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        chost, self._extra_headers, _x509 = self.get_host_info(host)
        self._connection = host, http.client.HTTPConnection(chost, timeout=self.timeout)
        return self._connection[1]


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """`SafeTransport` (https) that applies a socket timeout to its connection."""

    def __init__(self, *args: Any, timeout: float = DEFAULT_TIMEOUT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        chost, self._extra_headers, x509 = self.get_host_info(host)
        self._connection = host, http.client.HTTPSConnection(
            chost, None, context=self.context, timeout=self.timeout, **(x509 or {})
        )
        return self._connection[1]


def _make_proxy(url: str, timeout: float = DEFAULT_TIMEOUT) -> xmlrpc.client.ServerProxy:
    """Build a ServerProxy whose underlying socket honours `timeout`."""
    transport: xmlrpc.client.Transport
    if url.lower().startswith("https:"):
        transport = _TimeoutSafeTransport(timeout=timeout)
    else:
        transport = _TimeoutTransport(timeout=timeout)
    return xmlrpc.client.ServerProxy(url, allow_none=True, transport=transport)


#: Company the current tool call works in. Odoo scopes many models (accounts,
#: journals, taxes …) by ``allowed_company_ids`` in the context, so a call that
#: names a company must send it, or a multi-company user's default company
#: silently wins. Set with ``company_scope()``; read by every execute_kw.
_company_scope: contextvars.ContextVar[int | None] = contextvars.ContextVar("odoo_mcp_company", default=None)


@contextmanager
def company_scope(company_id: int | None):
    token = _company_scope.set(int(company_id) if company_id else None)
    try:
        yield
    finally:
        _company_scope.reset(token)


def enter_company_scope(company_id: int | None) -> None:
    """Non-context-manager form for tool bodies: scope the rest of this call.

    ContextVars are per task/thread, and FastMCP runs each tool call in its
    own; nothing leaks to the next request.
    """
    _company_scope.set(int(company_id) if company_id else None)


def _with_company(kwargs: dict[str, Any]) -> dict[str, Any]:
    cid = _company_scope.get()
    if not cid:
        return kwargs
    ctx = dict(kwargs.get("context") or {})
    ctx.setdefault("allowed_company_ids", [cid])
    ctx.setdefault("company_id", cid)
    return {**kwargs, "context": ctx}


class OdooClient:
    def __init__(self, instance: Instance, timeout: float = DEFAULT_TIMEOUT):
        self.instance = instance
        self.config = get_config(instance)
        self.timeout = timeout
        self._common_url = f"{self.config.url}/xmlrpc/2/common"
        self._models_url = f"{self.config.url}/xmlrpc/2/object"
        # Per-thread proxy storage. Never hand a proxy to another thread.
        self._local = threading.local()
        self._uid: int | None = None
        self._uid_lock = threading.Lock()

    @property
    def _common(self) -> xmlrpc.client.ServerProxy:
        proxy = getattr(self._local, "common", None)
        if proxy is None:
            proxy = _make_proxy(self._common_url, self.timeout)
            self._local.common = proxy
        return proxy

    @property
    def _models(self) -> xmlrpc.client.ServerProxy:
        proxy = getattr(self._local, "models", None)
        if proxy is None:
            proxy = _make_proxy(self._models_url, self.timeout)
            self._local.models = proxy
        return proxy

    @property
    def uid(self) -> int:
        # Double-checked: the fast path stays lock-free once authenticated, and
        # the lock keeps a burst of concurrent first-calls to one authenticate().
        if self._uid is None:
            with self._uid_lock:
                if self._uid is None:
                    uid = self._common.authenticate(
                        self.config.db, self.config.user, self.config.password, {}
                    )
                    if not uid:
                        raise RuntimeError(
                            f"Authentication failed for instance '{self.instance}' "
                            f"as {self.config.user}"
                        )
                    self._uid = uid
        assert self._uid is not None
        return self._uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Execute Odoo ORM method via XML-RPC.

        Example:
            client.execute_kw("res.partner", "search_read",
                              [[("name", "ilike", "Acme")]],
                              {"fields": ["id", "name"], "limit": 5})
        """
        kwargs = _with_company(kwargs or {})
        if self.config.impersonate:
            return self._execute_as_caller(model, method, args, kwargs)
        return self._models.execute_kw(
            self.config.db,
            self.uid,
            self.config.password,
            model,
            method,
            args,
            kwargs,
        )

    def _execute_as_caller(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        """Impersonation: route the call through the `odoo_mcp_gateway` addon as the
        OAuth caller. Fails closed: no authenticated caller → no call, never a
        silent fallback to the service account."""
        from odoo_mcp.auth import current_identity  # local import: auth imports instances

        identity = current_identity()
        # actor = email claim, else preferred_username/name, else sub. With Authentik's
        # sub_mode=user_email the sub *is* the login, so a sub-only token is fine too.
        if not identity.authenticated or not identity.actor:
            raise PermissionError(
                f"Instance '{self.instance}' runs calls as the caller, but the request "
                "carries no identity (no OAuth token, or a token without sub/email claims)."
            )
        return self._models.execute_kw(
            self.config.db,
            self.uid,
            self.config.password,
            "mcp.gateway",
            "execute_as",
            [identity.actor, model, method, args, kwargs],
            {},
        )


@lru_cache(maxsize=8)
def _client_for(instance: str) -> OdooClient:
    return OdooClient(instance)


def get_client(instance: Instance | None) -> OdooClient:
    """Cached client factory — one per instance; ``None`` means the only configured one.

    The client is shared across threads on purpose (it caches the uid); the
    non-thread-safe part, the ServerProxy, is thread-local inside it.
    """
    return _client_for(resolve_instance(instance))
