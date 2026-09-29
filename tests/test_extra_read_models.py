"""Per-instance extra read models (``ODOO_<NAME>_EXTRA_READ_MODELS``).

The extra list widens the generic readers on one instance only. What matters is
what it must *not* do: open a denied model, leak to another instance, or reach
any write path.
"""

from __future__ import annotations

import inspect

import pytest

from odoo_mcp import instances, server
from odoo_mcp.access import AccessDenied, check_model, parse_extra_read_models
from odoo_mcp.tools import attachments, expenses, write_critical, write_safe
from odoo_mcp.tools import read as read_module

EXTRA_KEY = "ODOO_ACME_EXTRA_READ_MODELS"


@pytest.fixture
def two_instances(monkeypatch):
    """Two configured instances, ``acme`` and ``other``; no extras set yet."""
    for k in list(__import__("os").environ):
        if k.startswith("ODOO_") and (k.endswith("_URL") or k.endswith("_EXTRA_READ_MODELS")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ODOO_ACME_URL", "https://acme.example")
    monkeypatch.setenv("ODOO_OTHER_URL", "https://other.example")
    return monkeypatch


@pytest.fixture
def patched(mock_client, monkeypatch):
    monkeypatch.setattr(read_module, "get_client", lambda inst: mock_client)
    return mock_client


class TestParsing:
    def test_unset_and_empty_mean_none(self):
        assert parse_extra_read_models(None, EXTRA_KEY) == frozenset()
        assert parse_extra_read_models("", EXTRA_KEY) == frozenset()
        assert parse_extra_read_models(" , ,", EXTRA_KEY) == frozenset()

    def test_trims_and_ignores_empty_entries(self):
        got = parse_extra_read_models(" acme.budget ,, acme.budget.line ,", EXTRA_KEY)
        assert got == frozenset({"acme.budget", "acme.budget.line"})

    @pytest.mark.parametrize(
        "bad",
        ["acme.*", "acme.", ".acme", "Acme.Budget", "acme..budget", "acme budget", "1acme.x", "acme/x"],
    )
    def test_rejects_anything_but_exact_names(self, bad):
        with pytest.raises(ValueError, match=EXTRA_KEY):
            parse_extra_read_models(f"acme.budget,{bad}", EXTRA_KEY)

    @pytest.mark.parametrize(
        "denied",
        [
            "res.users",
            "res.partner.bank",
            "mail.message",
            "ir.config_parameter",
            "ir.cron",  # not in DENIED_MODELS, caught by the ir. prefix
            "res.users.apikeys",
            "mail.mail",
            "auth_totp.device",
        ],
    )
    def test_rejects_denied_models(self, denied):
        with pytest.raises(ValueError, match="denied by policy"):
            parse_extra_read_models(denied, EXTRA_KEY)


class TestCheckModel:
    def test_denylist_wins_over_extra_list(self):
        # Even if a denied name reached check_model through some other path,
        # the denial is checked first.
        for denied in ("res.users", "res.partner.bank", "mail.message", "ir.cron"):
            with pytest.raises(AccessDenied):
                check_model(denied, extra_read_models={denied})

    def test_extra_model_allowed_only_when_listed(self):
        check_model("x_custom.model", extra_read_models={"x_custom.model"})
        with pytest.raises(AccessDenied, match="EXTRA_READ_MODELS"):
            check_model("x_custom.model")

    def test_exact_match_not_prefix(self):
        with pytest.raises(AccessDenied):
            check_model("acme.budget.line", extra_read_models={"acme.budget"})


class TestInstanceBinding:
    def test_extra_model_readable_on_its_instance(self, two_instances, patched):
        two_instances.setenv(EXTRA_KEY, "acme.budget, acme.budget.line")
        patched.state = {"acme.budget": {"search_read": [{"id": 1, "name": "2026"}]}}
        rows = read_module.odoo_search_read(instance="acme", model="acme.budget", fields=["id", "name"])
        assert rows == [{"id": 1, "name": "2026"}]
        read_module.odoo_read_group(instance="acme", model="acme.budget.line", groupby=["budget_id"])
        read_module.odoo_fields_get(instance="acme", model="acme.budget")

    def test_extra_model_denied_on_other_instance(self, two_instances, patched):
        two_instances.setenv(EXTRA_KEY, "acme.budget")
        for call in (
            lambda: read_module.odoo_search_read(instance="other", model="acme.budget"),
            lambda: read_module.odoo_read_group(instance="other", model="acme.budget", groupby=["x"]),
            lambda: read_module.odoo_fields_get(instance="other", model="acme.budget"),
        ):
            with pytest.raises(AccessDenied):
                call()
        assert patched.calls == []

    def test_unresolved_instance_gets_no_extras(self, two_instances, patched):
        # Several instances and none named: the shared policy applies alone.
        two_instances.setenv(EXTRA_KEY, "acme.budget")
        with pytest.raises(AccessDenied):
            read_module.odoo_search_read(model="acme.budget")

    def test_denied_fields_still_scrubbed_on_extra_model(self, two_instances, patched):
        two_instances.setenv(EXTRA_KEY, "acme.budget")
        patched.state = {"acme.budget": {"search_read": [{"id": 1, "access_token": "t", "datas": "x"}]}}
        assert read_module.odoo_search_read(instance="acme", model="acme.budget") == [{"id": 1}]
        with pytest.raises(AccessDenied):
            read_module.odoo_search_read(instance="acme", model="acme.budget", fields=["access_token"])

    def test_unset_means_unchanged_behaviour(self, two_instances, patched):
        assert instances.extra_read_models("acme") == frozenset()
        with pytest.raises(AccessDenied):
            read_module.odoo_search_read(instance="acme", model="acme.budget")
        read_module.odoo_search_read(instance="acme", model="account.move")  # base policy intact


class TestNoWritePath:
    WRITE_MODULES = (write_safe, write_critical, expenses)

    def test_write_modules_never_consult_the_read_policy(self):
        # The extra list only reaches check_model, and only the read escape
        # hatches call it. A write module importing either would be a new path.
        for mod in (*self.WRITE_MODULES, attachments):
            src = inspect.getsource(mod)
            assert "extra_read_models" not in src, mod.__name__
            assert "check_model" not in src, mod.__name__

    def test_attachment_tools_do_not_serve_extra_models(self, two_instances, mock_client, monkeypatch):
        two_instances.setenv(EXTRA_KEY, "acme.budget")
        monkeypatch.setattr(attachments, "get_client", lambda inst: mock_client)
        with pytest.raises(ValueError, match="not served"):
            attachments.odoo_list_attachments(instance="acme", res_model="acme.budget", res_id=1)


class TestStartup:
    def test_validate_collects_every_instance(self, two_instances):
        two_instances.setenv(EXTRA_KEY, "acme.budget")
        assert instances.validate_extra_read_models() == {
            "acme": frozenset({"acme.budget"}),
            "other": frozenset(),
        }

    def test_invalid_value_stops_server_start(self, two_instances, monkeypatch):
        two_instances.setenv("ODOO_OTHER_EXTRA_READ_MODELS", "acme.*")
        ran = []
        monkeypatch.setattr(server.mcp, "run", lambda *a, **k: ran.append(True))
        with pytest.raises(ValueError, match="ODOO_OTHER_EXTRA_READ_MODELS"):
            server.main()
        assert ran == []

    def test_denied_model_stops_server_start(self, two_instances, monkeypatch):
        two_instances.setenv(EXTRA_KEY, "acme.budget,res.users")
        monkeypatch.setattr(server.mcp, "run", lambda *a, **k: None)
        with pytest.raises(ValueError, match="denied by policy"):
            server.main()
