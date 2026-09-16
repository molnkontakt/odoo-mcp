"""Tests for the read-access policy on the generic escape hatches.

These pin the boundary itself, not just the happy path: the point of the policy
is what it *refuses*, so most of these assert refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.access import AccessDenied, check_fields, check_model, scrub_rows
from odoo_mcp.tools import read as read_module


@pytest.fixture
def patched(mock_client, monkeypatch):
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(read_module, "get_client", lambda inst: mock_client)
    return mock_client


class TestModelPolicy:
    @pytest.mark.parametrize(
        "model",
        [
            "account.move",
            "account.move.line",
            "account.journal",
            "product.product",
            "res.partner",
            "res.country",
            "res.currency",
            "res.company",
            "ir.attachment",
            "uom.uom",
        ],
    )
    def test_accounting_domain_is_allowed(self, model):
        check_model(model)  # must not raise

    @pytest.mark.parametrize(
        "model",
        [
            "res.users",
            "res.groups",
            "res.partner.bank",
            "mail.message",
            "ir.config_parameter",
            "ir.mail_server",
            "ir.logging",
            "ir.model",
            "ir.rule",
        ],
    )
    def test_sensitive_models_are_denied(self, model):
        with pytest.raises(AccessDenied):
            check_model(model)

    def test_unknown_models_are_denied_by_default(self):
        # Default-deny: a model nobody thought about must fail closed, so a new
        # Odoo module cannot quietly widen the surface.
        with pytest.raises(AccessDenied, match="not in the allowed"):
            check_model("stock.picking")

    def test_denied_beats_allowed_prefix(self):
        """A denial must not be reachable via a prefix rule."""
        # res.partner is allowed exactly; res.partner.bank must still be denied.
        check_model("res.partner")
        with pytest.raises(AccessDenied, match="blocked by policy"):
            check_model("res.partner.bank")

    def test_prefix_match_requires_the_dot(self):
        with pytest.raises(AccessDenied):
            check_model("accountancy.report")

    def test_empty_model_is_denied(self):
        with pytest.raises(AccessDenied):
            check_model("")


class TestFieldPolicy:
    def test_requesting_a_denied_field_raises(self):
        with pytest.raises(AccessDenied, match="access_token"):
            check_fields(["id", "name", "access_token"])

    def test_payload_fields_are_denied(self):
        with pytest.raises(AccessDenied):
            check_fields(["datas"])
        with pytest.raises(AccessDenied):
            check_fields(["raw"])

    def test_groupby_suffix_is_stripped_before_checking(self):
        # read_group accepts "date:month"; the policy must look at the base name.
        with pytest.raises(AccessDenied):
            check_fields(["access_token:day"])

    def test_ordinary_fields_pass(self):
        check_fields(["id", "name", "date", "amount_total"])
        check_fields(None)

    def test_scrub_strips_denied_keys_from_rows(self):
        rows = [{"id": 1, "name": "invoice.pdf", "datas": "BASE64", "access_token": "t"}]
        assert scrub_rows(rows) == [{"id": 1, "name": "invoice.pdf"}]


class TestToolIntegration:
    def test_search_read_blocks_denied_model(self, patched):
        with pytest.raises(AccessDenied):
            read_module.odoo_search_read(instance="dev", model="res.users")
        # The call must be refused before it reaches Odoo.
        assert patched.calls == []

    def test_search_read_scrubs_even_when_no_fields_requested(self, patched):
        """Omitting `fields` makes Odoo return its default set.

        On ir.attachment that includes the payload, so scrubbing cannot depend
        on the caller having named the fields.
        """
        patched.state = {
            "ir.attachment": {
                "search_read": [
                    {"id": 5, "name": "faktura.pdf", "datas": "JVBERi0=", "access_token": "abc"}
                ]
            }
        }
        rows: list[dict[str, Any]] = read_module.odoo_search_read(
            instance="dev", model="ir.attachment"
        )
        assert rows == [{"id": 5, "name": "faktura.pdf"}]

    def test_read_group_checks_groupby_keys(self, patched):
        with pytest.raises(AccessDenied):
            read_module.odoo_read_group(
                instance="dev", model="ir.attachment", groupby=["access_token"]
            )
        assert patched.calls == []

    def test_fields_get_hides_denied_fields(self, patched):
        patched.state = {
            "ir.attachment": {
                "fields_get": {
                    "name": {"type": "char"},
                    "datas": {"type": "binary"},
                    "access_token": {"type": "char"},
                }
            }
        }
        result = read_module.odoo_fields_get(instance="dev", model="ir.attachment")
        assert set(result) == {"name"}

    def test_allowed_model_still_works(self, patched):
        patched.state = {"account.move": {"search_read": [{"id": 1, "name": "BILL/1"}]}}
        rows = read_module.odoo_search_read(
            instance="dev", model="account.move", fields=["id", "name"]
        )
        assert rows == [{"id": 1, "name": "BILL/1"}]
