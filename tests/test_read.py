"""Tests for read escape-hatch + metadata tools using MockClient."""

from __future__ import annotations

import pytest

from odoo_mcp.tools import read


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    """Replace get_client in the read module with the mock."""
    monkeypatch.setattr(read, "get_client", lambda inst: mock_client)
    return mock_client


class TestSearchRead:
    def test_passthrough(self, patched_client):
        patched_client.state = {
            "res.partner": {"search_read": [{"id": 1, "name": "Acme AB"}]}
        }
        rows = read.odoo_search_read(
            instance="dev",
            model="res.partner",
            domain=[["is_company", "=", True]],
            fields=["id", "name"],
            limit=10,
        )
        assert rows == [{"id": 1, "name": "Acme AB"}]
        model, method, args, kwargs = patched_client.calls[-1]
        assert model == "res.partner"
        assert method == "search_read"
        assert args == [[["is_company", "=", True]]]
        assert kwargs["fields"] == ["id", "name"]
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 0

    def test_domain_defaults_to_empty(self, patched_client):
        read.odoo_search_read(instance="dev", model="account.move")
        _, _, args, kwargs = patched_client.calls[-1]
        assert args == [[]]
        # no fields/order keys unless provided
        assert "fields" not in kwargs
        assert "order" not in kwargs


class TestReadGroup:
    def test_groups(self, patched_client):
        patched_client.state = {
            "account.move.line": {
                "read_group": [
                    {"account_id": [153, "2614 X"], "balance": 42.0, "__count": 3}
                ]
            }
        }
        rows = read.odoo_read_group(
            instance="dev",
            model="account.move.line",
            groupby=["account_id"],
            fields=["balance"],
            domain=[["parent_state", "=", "posted"]],
        )
        assert rows[0]["__count"] == 3
        _, method, args, kwargs = patched_client.calls[-1]
        assert method == "read_group"
        assert args == [[["parent_state", "=", "posted"]], ["balance"], ["account_id"]]
        assert kwargs["lazy"] is False

    def test_empty_domain_and_fields(self, patched_client):
        read.odoo_read_group(instance="dev", model="account.move.line", groupby=["journal_id"])
        _, _, args, _ = patched_client.calls[-1]
        assert args == [[], [], ["journal_id"]]


class TestFieldsGet:
    def test_default_attributes(self, patched_client):
        patched_client.state = {
            "account.move": {"fields_get": {"state": {"type": "selection"}}}
        }
        out = read.odoo_fields_get(instance="dev", model="account.move")
        assert "state" in out
        _, method, args, kwargs = patched_client.calls[-1]
        assert method == "fields_get"
        assert "string" in kwargs["attributes"]
        assert "type" in kwargs["attributes"]


class TestMetadataReaders:
    def test_list_journals(self, patched_client):
        patched_client.state = {
            "account.journal": {
                "search_read": [
                    {"id": 1, "code": "BNK1", "name": "Swedbank", "type": "bank"}
                ]
            }
        }
        rows = read.odoo_list_journals(instance="dev")
        assert rows[0]["code"] == "BNK1"

    def test_list_accounts_query_and_type(self, patched_client):
        patched_client.state = {"account.account": {"search_read": []}}
        read.odoo_list_accounts(
            instance="dev", query="193", account_type="asset_cash"
        )
        _, _, args, kwargs = patched_client.calls[-1]
        domain = args[0]
        assert ("account_type", "=", "asset_cash") in domain
        assert kwargs["order"] == "code"

    def test_list_tax_tags_filters_taxes(self, patched_client):
        patched_client.state = {"account.account.tag": {"search_read": []}}
        read.odoo_list_tax_tags(instance="dev")
        _, _, args, _ = patched_client.calls[-1]
        assert args == [[("applicability", "=", "taxes")]]

    def test_list_products_query(self, patched_client):
        patched_client.state = {"product.product": {"search_read": []}}
        read.odoo_list_products(instance="dev", query="konsult")
        _, _, args, kwargs = patched_client.calls[-1]
        assert kwargs["limit"] == 50
        # OR domain on name/default_code
        assert "|" in args[0]
