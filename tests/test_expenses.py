"""Expense tools: lookups stay narrow, creation is draft-only and takes the receipt with it."""

from __future__ import annotations

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.tools import expenses
from odoo_mcp.validators import ValidationError

EMPLOYEE = {"id": 2, "name": "Benny Example", "company_id": [1, "Example Road Association"]}
CATEGORY = {"id": 7, "name": "Green areas – material", "default_code": "GRON", "company_id": False}


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(expenses, "get_client", lambda inst: mock_client)
    return mock_client


def _state(**overrides):
    state = {
        "hr.employee": {"read": [EMPLOYEE]},
        "product.product": {"search_read": [CATEGORY]},
        "hr.expense": {
            "create": 501,
            "read": [{"id": 501, "name": "JULA receipt 075797932", "state": "draft", "total_amount": 249.4,
                      "date": "2026-09-10", "currency_id": [18, "SEK"]}],
            "write": True,
        },
        "ir.attachment": {"create": 1100},
    }
    state.update(overrides)
    return state


class TestLookups:
    def test_list_employees_returns_only_the_four_fields(self, patched_client):
        patched_client.state = {"hr.employee": {"search_read": [
            {"id": 2, "name": "Benny Example", "company_id": [1, "Road"], "work_email": "b@example.test",
             "private_street": "should never be here"},
        ]}}
        rows = expenses.odoo_list_employees(query="benny", company_id=1, instance="dev")
        assert rows == [{"employee_id": 2, "name": "Benny Example", "company": {"id": 1, "name": "Road"},
                         "work_email": "b@example.test"}]
        _, _, args, kwargs = patched_client.calls[-1]
        assert ("name", "ilike", "benny") in args[0] and ("company_id", "=", 1) in args[0]
        assert kwargs["fields"] == ["id", "name", "company_id", "work_email"]

    def test_list_expense_categories_includes_shared_ones(self, patched_client):
        patched_client.state = {"product.product": {"search_read": [CATEGORY]}}
        rows = expenses.odoo_list_expense_categories(company_id=1, instance="dev")
        assert rows == [{"product_id": 7, "code": "GRON", "name": "Green areas – material", "company": None}]
        _, _, args, _ = patched_client.calls[-1]
        assert ("can_be_expensed", "=", True) in args[0]
        assert ("company_id", "in", [False, 1]) in args[0]


class TestCreateExpense:
    def test_creates_draft_with_receipt_as_main_attachment(self, patched_client):
        patched_client.state = _state()
        result = expenses.odoo_create_expense(
            employee_id=2, name="JULA receipt 075797932", total_amount=249.4, date="2026-09-10",
            category_code="GRON", receipt_base64="AAAA", receipt_filename="kvitto.jpg",
            receipt_mimetype="image/jpeg", instance="dev",
        )
        assert result["expense_id"] == 501 and result["state"] == "draft"
        assert result["attachment_id"] == 1100
        assert result["company"] == {"id": 1, "name": "Example Road Association"}
        assert result["category"] == {"id": 7, "code": "GRON", "name": "Green areas – material"}
        assert result["currency"] == "SEK"

        create = next(c for c in patched_client.calls if c[0] == "hr.expense" and c[1] == "create")
        vals = create[2][0]
        assert vals["company_id"] == 1, "company comes from the employee, not the caller"
        assert vals["product_id"] == 7 and vals["employee_id"] == 2
        assert vals["total_amount_currency"] == 249.4 and vals["payment_mode"] == "own_account"
        assert "state" not in vals

        att = next(c for c in patched_client.calls if c[0] == "ir.attachment" and c[1] == "create")
        assert att[2][0] == {"name": "kvitto.jpg", "res_model": "hr.expense", "res_id": 501,
                             "datas": "AAAA", "mimetype": "image/jpeg"}
        write = next(c for c in patched_client.calls if c[0] == "hr.expense" and c[1] == "write")
        assert write[2] == [[501], {"message_main_attachment_id": 1100}]

    def test_category_lookup_is_scoped_to_the_employees_company(self, patched_client):
        patched_client.state = _state()
        expenses.odoo_create_expense(employee_id=2, name="x", total_amount=1, date="2026-09-10",
                                     product_id=7, instance="dev")
        lookup = next(c for c in patched_client.calls if c[0] == "product.product")
        assert ("company_id", "in", [False, 1]) in lookup[2][0]
        assert ("can_be_expensed", "=", True) in lookup[2][0]
        assert ("id", "=", 7) in lookup[2][0]

    def test_without_receipt_no_attachment_calls(self, patched_client):
        patched_client.state = _state()
        result = expenses.odoo_create_expense(employee_id=2, name="x", total_amount=10, date="2026-09-10",
                                              category_code="GRON", instance="dev")
        assert result["attachment_id"] is None
        assert not [c for c in patched_client.calls if c[0] == "ir.attachment"]

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"name": " "}, "name"),
            ({"total_amount": 0}, "positive"),
            ({"payment_mode": "cash"}, "payment_mode"),
            ({"receipt_base64": "AAAA"}, "receipt_filename"),
            ({"receipt_filename": "k.jpg"}, "receipt_base64"),
        ],
    )
    def test_rejects_bad_input_before_touching_odoo(self, patched_client, kwargs, match):
        patched_client.state = _state()
        base = {"employee_id": 2, "name": "x", "total_amount": 10, "date": "2026-09-10",
                "category_code": "GRON", "instance": "dev"}
        with pytest.raises(ValidationError, match=match):
            expenses.odoo_create_expense(**{**base, **kwargs})
        assert patched_client.calls == []

    def test_unknown_employee(self, patched_client):
        patched_client.state = _state(**{"hr.employee": {"read": []}})
        with pytest.raises(ValidationError, match="Employee 99"):
            expenses.odoo_create_expense(employee_id=99, name="x", total_amount=10, date="2026-09-10",
                                         category_code="GRON", instance="dev")

    def test_unknown_or_ambiguous_category(self, patched_client):
        patched_client.state = _state(**{"product.product": {"search_read": []}})
        with pytest.raises(ValidationError, match="No expense category"):
            expenses.odoo_create_expense(employee_id=2, name="x", total_amount=10, date="2026-09-10",
                                         category_code="NOPE", instance="dev")
        patched_client.state = _state(**{"product.product": {"search_read": [CATEGORY, {**CATEGORY, "id": 8}]}})
        with pytest.raises(ValidationError, match="several"):
            expenses.odoo_create_expense(employee_id=2, name="x", total_amount=10, date="2026-09-10",
                                         category_code="GRON", instance="dev")
        assert not [c for c in patched_client.calls if c[0] == "hr.expense"]

    def test_category_or_product_required(self, patched_client):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match="category_code or product_id"):
            expenses.odoo_create_expense(employee_id=2, name="x", total_amount=10, date="2026-09-10", instance="dev")
