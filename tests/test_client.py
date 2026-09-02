"""Tests for the XML-RPC client — thread isolation and socket timeouts.

The defect these pin: `ServerProxy` holds one `Transport`, which caches one
`HTTPConnection` for keep-alive. Sharing that across threads interleaves two
requests on one socket; `http.client` raises `CannotSendRequest` on the loser,
and `Transport.single_request` closes the shared connection on any exception —
potentially yanking the socket out from under a thread whose *write* is already
on the wire. FastMCP dispatches sync tools into a worker thread pool, so this is
the normal case, not an exotic one.
"""

from __future__ import annotations

import threading
import xmlrpc.client
from typing import Any

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.client import (
    DEFAULT_TIMEOUT,
    OdooClient,
    _make_proxy,
    _TimeoutSafeTransport,
    _TimeoutTransport,
)
from odoo_mcp.instances import OdooConfig


@pytest.fixture
def offline_client(monkeypatch) -> OdooClient:
    """An OdooClient whose config resolves without touching the environment.

    No network call happens: the proxies are built lazily and we only inspect
    their identity, never call through them.
    """
    monkeypatch.setattr(
        client_module,
        "get_config",
        lambda instance: OdooConfig(
            url="https://odoo.invalid", db="testdb", user="u", password="p"
        ),
    )
    return OdooClient("dev")


class TestThreadIsolation:
    def test_each_thread_gets_its_own_proxy(self, offline_client):
        # Hold the proxy OBJECTS, not their id()s: a finished thread releases
        # its thread-local storage, and CPython happily reuses the address —
        # which made an id()-based version of this test flake.
        grabbed: list[Any] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def grab() -> None:
            try:
                # Line all threads up so they contend, rather than running
                # one after another and trivially passing.
                barrier.wait(timeout=5)
                proxy = offline_client._models
                with lock:
                    grabbed.append(proxy)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(grabbed) == 8
        # The whole point: eight threads, eight distinct proxy objects, all
        # still referenced here so identity is meaningful.
        assert len({id(p) for p in grabbed}) == 8

    def test_same_thread_reuses_its_proxy(self, offline_client):
        first = offline_client._models
        assert offline_client._models is first
        # …and the common proxy is a separate object from the models proxy.
        assert offline_client._common is not first

    def test_uid_is_authenticated_once_under_contention(self, monkeypatch, offline_client):
        """Concurrent first-calls must not fan out into N authenticate() calls."""
        auth_calls: list[tuple[Any, ...]] = []
        lock = threading.Lock()

        class FakeCommon:
            def authenticate(self, *args: Any) -> int:
                with lock:
                    auth_calls.append(args)
                return 42

        monkeypatch.setattr(
            type(offline_client),
            "_common",
            property(lambda self: FakeCommon()),
        )

        results: list[int] = []
        barrier = threading.Barrier(8)

        def grab() -> None:
            barrier.wait(timeout=5)
            results.append(offline_client.uid)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results == [42] * 8
        assert len(auth_calls) == 1


class TestTimeouts:
    def test_https_url_gets_a_safe_transport_with_timeout(self):
        proxy = _make_proxy("https://odoo.invalid/xmlrpc/2/object")
        transport = proxy("transport")  # ServerProxy exposes it via __call__
        assert isinstance(transport, _TimeoutSafeTransport)
        assert transport.timeout == DEFAULT_TIMEOUT

    def test_http_url_gets_a_plain_transport_with_timeout(self):
        proxy = _make_proxy("http://odoo.invalid/xmlrpc/2/object", timeout=5)
        transport = proxy("transport")
        assert isinstance(transport, _TimeoutTransport)
        assert not isinstance(transport, xmlrpc.client.SafeTransport)
        assert transport.timeout == 5

    def test_connection_is_built_with_the_timeout(self):
        transport = _TimeoutTransport(timeout=7)
        conn = transport.make_connection("odoo.invalid")
        assert conn.timeout == 7
        # Cached per transport, as upstream does for keep-alive.
        assert transport.make_connection("odoo.invalid") is conn
