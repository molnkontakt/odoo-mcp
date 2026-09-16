"""Fixed-window per-IP limit on the OAuth endpoints."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from odoo_mcp.ratelimit import RateLimitMiddleware


def _app(per_minute: int, trust_xff: bool = False) -> TestClient:
    async def ok(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/register", ok, methods=["POST"]), Route("/token", ok, methods=["POST"]), Route("/mcp", ok, methods=["POST"])])
    limited = RateLimitMiddleware(app, per_minute=per_minute, paths=("/register", "/token"), trust_forwarded_for=trust_xff)
    return TestClient(limited)


def test_blocks_after_limit_with_retry_after():
    c = _app(3)
    assert [c.post("/register").status_code for _ in range(3)] == [200, 200, 200]
    r = c.post("/register")
    assert r.status_code == 429 and r.json()["error"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


def test_shared_budget_across_guarded_paths_but_not_mcp():
    c = _app(2)
    assert c.post("/register").status_code == 200
    assert c.post("/token").status_code == 200
    assert c.post("/token").status_code == 429
    # the MCP endpoint itself is never limited here
    assert all(c.post("/mcp").status_code == 200 for _ in range(10))


def test_forwarded_for_only_when_trusted():
    untrusted = _app(1, trust_xff=False)
    assert untrusted.post("/register", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert untrusted.post("/register", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429  # same real client
    trusted = _app(1, trust_xff=True)
    assert trusted.post("/register", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
    assert trusted.post("/register", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200  # separate buckets


def test_zero_disables():
    c = _app(0)
    assert all(c.post("/register").status_code == 200 for _ in range(50))
