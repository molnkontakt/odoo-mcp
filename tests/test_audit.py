"""Tests for the audit logger — focuses on no-op behavior when the env var
is unset (the only path we can test without a real Postgres)."""

from __future__ import annotations

import pytest

from odoo_mcp import audit


@pytest.fixture(autouse=True)
def _reset_logger_singleton(monkeypatch):
    monkeypatch.setattr(audit, "_logger_instance", None)


def test_disabled_when_env_missing(monkeypatch):
    monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
    logger = audit.get_logger()
    assert logger.enabled is False
    # Should silently no-op rather than raise:
    logger.record(tool="t", instance="dev", params={})


def test_audit_call_records_summary(monkeypatch):
    monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
    with audit.audit_call(tool="t", instance="dev", params={}) as ctx:
        ctx.summary = "ok"
    # No assertion on DB — just that nothing raised.


def test_audit_call_propagates_exception(monkeypatch):
    monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
    with (
        pytest.raises(RuntimeError, match="boom"),
        audit.audit_call(tool="t", instance="dev", params={}),
    ):
        raise RuntimeError("boom")


def test_enabled_when_env_set_but_no_psycopg(monkeypatch):
    """If env var is set but psycopg2 import fails or connect fails, the
    logger should fall back to disabled state silently."""
    monkeypatch.setenv("MCP_AUDIT_DB_URL", "postgresql://nowhere/x")

    # Force AuditLogger._connect to fail
    def _failing_connect(self):
        return None

    monkeypatch.setattr(audit.AuditLogger, "_connect", _failing_connect)

    logger = audit.get_logger()
    assert logger.enabled is True
    # First record() will trigger _connect, get None, and disable
    logger.record(tool="t", instance="dev", params={})
    assert logger.enabled is False
