"""Task-shaped receivable readers: grouping, overdue arithmetic, optional-field probing."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from odoo_mcp.tools import receivables


@pytest.fixture
def fake_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(receivables, "get_client", lambda instance: client)
    return client


def _route(fields_present, rows):
    def execute_kw(model, method, args, kwargs=None):
        if method == "fields_get":
            return {f: {"type": "x"} for f in args[0] if f in fields_present}
        if method == "search_read":
            return rows
        if method == "read":
            return [{"id": 7, "name": "Example Street 2", "email": "hv2@example", "phone": False}]
        raise AssertionError((model, method))
    return execute_kw


def test_overdue_invoices_days_and_reminder_fields(fake_client):
    fake_client.execute_kw.side_effect = _route(
        {"reminder_level_id", "reminder_date", "reminder_count", "reminder_next_level_id"},
        [{"id": 1, "name": "INV/2026/0001", "ref": "x", "partner_id": [7, "Example Street 2"], "company_id": [2, "Example Co"],
          "invoice_date": "2026-09-11", "invoice_date_due": "2026-10-11", "amount_total": 1000.0, "amount_residual": 1000.0,
          "currency_id": [18, "SEK"], "reminder_level_id": False, "reminder_date": False, "reminder_count": 0,
          "reminder_next_level_id": [1, "Påminnelse"]}],
    )
    res = receivables.odoo_overdue_invoices(instance="dev", as_of="2026-10-25")
    assert res["count"] == 1 and res["total_residual"] == 1000.0
    inv = res["invoices"][0]
    assert inv["days_overdue"] == 14
    assert inv["next_reminder_level"] == "Påminnelse"
    assert inv["last_reminder"] == {"level": None, "date": None, "count": 0}


def test_overdue_invoices_without_reminder_module(fake_client):
    fake_client.execute_kw.side_effect = _route(set(), [
        {"id": 1, "name": "INV/1", "ref": None, "partner_id": [7, "A"], "company_id": [1, "C"], "invoice_date": "2026-01-01",
         "invoice_date_due": "2026-01-31", "amount_total": 10.0, "amount_residual": 4.0, "currency_id": [1, "SEK"]}])
    res = receivables.odoo_overdue_invoices(instance="dev", as_of="2026-02-10", min_days_overdue=5)
    assert res["invoices"][0]["days_overdue"] == 10
    assert "last_reminder" not in res["invoices"][0]
    # requested fields must not include the optional ones when absent
    search_call = [c for c in fake_client.execute_kw.call_args_list if c.args[1] == "search_read"][-1]
    assert "reminder_level_id" not in search_call.args[3]["fields"]


def test_overdue_min_days_filter(fake_client):
    fake_client.execute_kw.side_effect = _route(set(), [
        {"id": 1, "name": "A", "ref": None, "partner_id": [1, "p"], "company_id": [1, "c"], "invoice_date": "2026-01-01",
         "invoice_date_due": "2026-02-08", "amount_total": 1, "amount_residual": 1, "currency_id": [1, "SEK"]}])
    assert receivables.odoo_overdue_invoices(instance="dev", as_of="2026-02-10", min_days_overdue=5)["count"] == 0


def test_unpaid_by_customer_groups_and_sorts(fake_client, monkeypatch):
    monkeypatch.setattr(receivables, "date", type("D", (), {"today": staticmethod(lambda: date(2026, 10, 20)),
                                                            "fromisoformat": staticmethod(date.fromisoformat)}))
    fake_client.execute_kw.side_effect = _route(set(), [
        {"id": 1, "name": "INV/1", "partner_id": [7, "Zeta"], "invoice_date_due": "2026-11-01", "amount_residual": 100.0, "company_id": [2, "Example Co"]},
        {"id": 2, "name": "INV/2", "partner_id": [8, "Alfa"], "invoice_date_due": "2026-10-11", "amount_residual": 50.0, "company_id": [2, "Example Co"]},
        {"id": 3, "name": "INV/3", "partner_id": [8, "Alfa"], "invoice_date_due": "2026-10-15", "amount_residual": 25.0, "company_id": [2, "Example Co"]},
    ])
    res = receivables.odoo_unpaid_by_customer(instance="dev")
    assert res["customers"] == 2 and res["invoices"] == 3 and res["total_residual"] == 175.0
    first = res["by_customer"][0]
    assert first["partner"]["name"] == "Alfa" and first["overdue"] is True and first["total_residual"] == 75.0
    assert first["oldest_due"] == "2026-10-11"
    assert res["by_customer"][1]["overdue"] is False


def test_unreconciled_bank_lines_net_by_journal(fake_client):
    fake_client.execute_kw.side_effect = _route(set(), [
        {"id": 1, "date": "2026-09-01", "payment_ref": "Swish", "amount": 300.0, "partner_id": False, "journal_id": [18, "Swedbank"], "statement_id": [4, "S"], "company_id": [2, "Example Co"]},
        {"id": 2, "date": "2026-09-02", "payment_ref": "Bg", "amount": -50.0, "partner_id": [3, "P"], "journal_id": [18, "Swedbank"], "statement_id": [4, "S"], "company_id": [2, "Example Co"]},
    ])
    res = receivables.odoo_unreconciled_bank_lines(instance="dev", journal_code="BNK1")
    assert res["count"] == 2 and res["net_by_journal"] == {"Swedbank": 250.0}
    domain = fake_client.execute_kw.call_args[0][2][0]
    assert ("journal_id.code", "=", "BNK1") in domain and ("is_reconciled", "=", False) in domain


def test_customer_statement_signs_refunds(fake_client):
    fake_client.execute_kw.side_effect = _route(set(), [
        {"id": 1, "name": "INV/1", "ref": None, "move_type": "out_invoice", "invoice_date": "2026-09-01", "invoice_date_due": "2026-10-01",
         "amount_total": 1000.0, "amount_residual": 1000.0, "payment_state": "not_paid", "company_id": [2, "Example Co"]},
        {"id": 2, "name": "RINV/1", "ref": None, "move_type": "out_refund", "invoice_date": "2026-09-02", "invoice_date_due": "2026-09-02",
         "amount_total": 300.0, "amount_residual": 300.0, "payment_state": "not_paid", "company_id": [2, "Example Co"]},
    ])
    res = receivables.odoo_customer_statement(instance="dev", partner_id=7)
    assert res["partner"]["name"] == "Example Street 2"
    assert [d["amount_residual"] for d in res["documents"]] == [1000.0, -300.0]
    assert res["balance_due"] == 700.0
