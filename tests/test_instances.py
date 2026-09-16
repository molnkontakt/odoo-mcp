"""Instance discovery and the single-instance default."""

from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch, env: dict[str, str]):
    for k in list(__import__("os").environ):
        if k.startswith("ODOO_") and k.endswith("_URL"):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import odoo_mcp.instances as inst
    return importlib.reload(inst)


def test_discovers_and_orders_instances(monkeypatch):
    inst = _reload(monkeypatch, {"ODOO_ACME_URL": "x", "ODOO_DEV_URL": "y", "ODOO_PROD_URL": "z"})
    assert inst.available_instances() == ("prod", "dev", "acme")
    assert inst.default_instance() is None
    with pytest.raises(ValueError, match="Several instances"):
        inst.resolve_instance(None)
    assert inst.resolve_instance("acme") == "acme"


def test_single_instance_is_default_and_production_flag(monkeypatch):
    inst = _reload(monkeypatch, {"ODOO_ACME_URL": "x", "ODOO_ACME_PRODUCTION": "1"})
    assert inst.available_instances() == ("acme",)
    assert inst.resolve_instance(None) == "acme"
    assert inst.is_production("acme") is True
    assert inst.is_production("dev") is False
    assert inst.is_production("prod") is True


def test_falls_back_to_prod_dev_when_nothing_configured(monkeypatch):
    inst = _reload(monkeypatch, {})
    assert inst.available_instances() == ("prod", "dev")
    _reload(monkeypatch, {"ODOO_DEV_URL": "y"})  # leave a sane module state for other tests


def test_critical_audit_refuses_null_instance(monkeypatch):
    """(instance, tool, key) is the idempotency index; NULL never equals NULL, so a
    critical row without an instance would silently disable replay protection."""
    import pytest

    from odoo_mcp import audit

    monkeypatch.setenv("MCP_AUDIT_DB_URL", "postgresql://x")
    with pytest.raises(audit.AuditUnavailable, match="resolved instance"), audit.audit_call(
        tool="odoo_post_journal_entry", instance=None, params={}, critical=True
    ):
        pass
