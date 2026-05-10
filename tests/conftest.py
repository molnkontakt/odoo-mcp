"""Shared pytest fixtures.

A `MockClient` stands in for `odoo_mcp.client.OdooClient` so the validators
and write tools can be tested without a live Odoo instance.
"""

from __future__ import annotations

from typing import Any

import pytest


class MockClient:
    """Minimal Odoo XML-RPC client substitute for tests.

    Set `state` to a dict like {model: {method: callable | data}} and
    execute_kw will resolve calls against it. Calls are recorded in `.calls`.
    """

    def __init__(self, state: dict[str, Any] | None = None):
        self.state: dict[str, Any] = state or {}
        self.calls: list[tuple[str, str, list[Any], dict[str, Any]]] = []

    def execute_kw(
        self,
        model: str,
        method: str,
        args: list[Any],
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append((model, method, args, kwargs or {}))
        per_model = self.state.get(model, {})
        handler = per_model.get(method)
        if callable(handler):
            return handler(args, kwargs or {})
        if handler is not None:
            return handler
        # Sensible defaults
        if method == "search_read":
            return []
        if method == "read":
            return []
        if method == "create":
            return 12345
        if method == "write":
            return True
        return None


@pytest.fixture
def mock_client() -> MockClient:
    return MockClient()
