"""Company scoping on the read tools: filter, output, and refusal of ambiguous account codes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from odoo_mcp.tools import read


@pytest.fixture
def client(monkeypatch):
    c = MagicMock()
    monkeypatch.setattr(read, "get_client", lambda instance: c)
    return c


def test_search_journal_entries_filters_and_returns_company(client):
    client.execute_kw.return_value = []
    read.odoo_search_journal_entries(company_id=2, instance="dev")
    model, method, args, kwargs = client.execute_kw.call_args[0]
    assert ("company_id", "=", 2) in args[0] and "company_id" in kwargs["fields"]


def test_search_invoices_returns_company(client):
    client.execute_kw.return_value = []
    read.odoo_search_invoices(date_from="2026-01-01", date_to="2026-12-31", company_id=1, instance="dev")
    _, _, args, kwargs = client.execute_kw.call_args[0]
    assert ("company_id", "=", 1) in args[0] and "company_id" in kwargs["fields"]


def test_account_balance_refuses_ambiguous_code(client):
    client.execute_kw.return_value = [
        {"id": 1, "code": "1930", "name": "Bank", "company_ids": [1]},
        {"id": 2, "code": "1930", "name": "Bank", "company_ids": [2]},
    ]
    with pytest.raises(ValueError, match="several companies"):
        read.odoo_get_account_balance(account_code="1930", instance="dev")


def test_account_balance_scoped_by_company(client):
    def ex(model, method, args, kwargs=None):
        if model == "account.account":
            assert ("company_ids", "in", [2]) in args[0]
            return [{"id": 9, "code": "1930", "name": "Bank", "company_ids": [2]}]
        return [{"debit": 10.0, "credit": 4.0}]
    client.execute_kw.side_effect = ex
    res = read.odoo_get_account_balance(account_code="1930", company_id=2, instance="dev")
    assert res["balance"] == 6.0


def test_aggregate_refuses_ambiguous_codes(client):
    client.execute_kw.return_value = [
        {"id": 1, "code": "3014", "name": "x", "company_ids": [1]},
        {"id": 2, "code": "3014", "name": "x", "company_ids": [2]},
    ]
    with pytest.raises(ValueError, match="3014"):
        read.odoo_query_account_aggregate(account_codes=["3014"], date_from="2026-01-01", date_to="2026-12-31", instance="dev")
