"""Tests for the Phase 1b write_safe tools (invoicing, partner, product, attachment)."""

from __future__ import annotations

import pytest

from odoo_mcp.access import AccessDenied
from odoo_mcp.tools import write_safe
from odoo_mcp.validators import ValidationError


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    monkeypatch.setattr(write_safe, "get_client", lambda inst: mock_client)
    return mock_client


class TestCreateInvoice:
    def _base_state(self):
        return {
            "res.partner": {"read": [{"id": 7, "name": "Acme AB"}]},
            "account.account": {"search_read": [{"id": 100, "code": "3010"}]},
            "account.tax": {
                "search_read": [
                    {"id": 1, "name": "Utg moms 25%", "type_tax_use": "sale"}
                ]
            },
            "account.move": {
                "create": 555,
                "read": [{
                    "id": 555, "name": "INV/2026/0001", "state": "draft",
                    "move_type": "out_invoice", "amount_untaxed": 1000.0,
                    "amount_tax": 250.0, "amount_total": 1250.0,
                }],
            },
        }

    def test_happy_path_out_invoice(self, patched_client):
        patched_client.state = self._base_state()
        result = write_safe.odoo_create_invoice(
            instance="dev",
            move_type="out_invoice",
            partner_id=7,
            lines=[{
                "name": "Konsulttimmar", "price_unit": 1000, "quantity": 1,
                "account_code": "3010", "tax_names": ["Utg moms 25%"],
            }],
        )
        assert result["move_id"] == 555
        assert result["amount_total"] == 1250.0
        assert result["line_count"] == 1

        create_calls = [
            c for c in patched_client.calls
            if c[0] == "account.move" and c[1] == "create"
        ]
        assert len(create_calls) == 1
        move_vals = create_calls[0][2][0]
        assert move_vals["move_type"] == "out_invoice"
        assert len(move_vals["invoice_line_ids"]) == 1
        line_vals = move_vals["invoice_line_ids"][0][2]
        assert line_vals["account_id"] == 100
        assert line_vals["tax_ids"] == [(6, 0, [1])]

    def test_wrong_direction_tax_rejected(self, patched_client):
        state = self._base_state()
        # The named tax is a purchase tax → the direction-constrained lookup
        # (sale/none) finds nothing → rejected before any create.
        state["account.tax"] = {"search_read": []}
        patched_client.state = state
        with pytest.raises(ValidationError, match="sale"):
            write_safe.odoo_create_invoice(
                instance="dev",
                move_type="out_invoice",
                partner_id=7,
                lines=[{
                    "name": "x", "price_unit": 100,
                    "tax_names": ["Ing moms 25%"],
                }],
            )
        assert not any(c[1] == "create" for c in patched_client.calls)

    def test_bad_move_type_rejected(self, patched_client):
        patched_client.state = self._base_state()
        with pytest.raises(ValidationError, match="move_type"):
            write_safe.odoo_create_invoice(
                instance="dev", move_type="banana", partner_id=7,
                lines=[{"name": "x", "price_unit": 1}],
            )

    def test_empty_lines_rejected(self, patched_client):
        patched_client.state = self._base_state()
        with pytest.raises(ValidationError, match="at least one line"):
            write_safe.odoo_create_invoice(
                instance="dev", move_type="out_invoice", partner_id=7, lines=[],
            )


class TestUpdateInvoice:
    def test_happy_path(self, patched_client):
        patched_client.state = {"account.move": {"read": [{"state": "draft"}], "write": True}}
        result = write_safe.odoo_update_invoice(
            instance="dev", move_id=555, values={"ref": "PO-42"}
        )
        assert result["updated"] == ["ref"]

    def test_posted_rejected(self, patched_client):
        patched_client.state = {"account.move": {"read": [{"state": "posted"}]}}
        with pytest.raises(ValidationError, match="not a draft"):
            write_safe.odoo_update_invoice(
                instance="dev", move_id=555, values={"ref": "PO-42"}
            )

    def test_disallowed_field_rejected(self, patched_client):
        patched_client.state = {"account.move": {"read": [{"state": "draft"}]}}
        with pytest.raises(ValidationError, match="Cannot update"):
            write_safe.odoo_update_invoice(
                instance="dev", move_id=555, values={"amount_total": 999}
            )


class TestCreatePartner:
    def test_with_country_and_ranks(self, patched_client):
        patched_client.state = {
            "res.country": {"search_read": [{"id": 68}]},
            "res.partner": {"create": 999},
        }
        result = write_safe.odoo_create_partner(
            instance="dev", name="Example Trading AB", vat="SE556000000001",
            country_code="se", customer=True,
        )
        assert result["partner_id"] == 999
        create_call = next(
            c for c in patched_client.calls
            if c[0] == "res.partner" and c[1] == "create"
        )
        vals = create_call[2][0]
        assert vals["vat"] == "SE556000000001"
        assert vals["country_id"] == 68
        assert vals["customer_rank"] == 1

    def test_unknown_country_rejected(self, patched_client):
        patched_client.state = {"res.country": {"search_read": []}}
        with pytest.raises(ValidationError, match="Country code"):
            write_safe.odoo_create_partner(
                instance="dev", name="X", country_code="ZZ"
            )


class TestCreateProduct:
    def test_happy_path(self, patched_client):
        patched_client.state = {"product.product": {"create": 321}}
        result = write_safe.odoo_create_product(
            instance="dev", name="Konsulttimme", list_price=1200, default_code="KONS"
        )
        assert result["product_id"] == 321
        vals = patched_client.calls[-1][2][0]
        assert vals["type"] == "service"
        assert vals["default_code"] == "KONS"


class TestUploadAttachment:
    def test_happy_path(self, patched_client):
        patched_client.state = {"ir.attachment": {"create": 777}}
        result = write_safe.odoo_upload_attachment(
            instance="dev", res_model="account.move", res_id=555,
            filename="bill.pdf", data_base64="JVBERi0=", mimetype="application/pdf",
        )
        assert result["attachment_id"] == 777
        vals = patched_client.calls[-1][2][0]
        assert vals["res_model"] == "account.move"
        assert vals["res_id"] == 555
        assert vals["datas"] == "JVBERi0="
        assert result["main_attachment"] is False
        assert not [c for c in patched_client.calls if c[1] == "write"], "no main attachment unless asked"

    def test_set_as_main_writes_the_record(self, patched_client):
        patched_client.state = {
            "ir.attachment": {"create": 778},
            "hr.expense": {"write": True, "fields_get": {"message_main_attachment_id": {"type": "many2one"}}},
        }
        result = write_safe.odoo_upload_attachment(
            instance="dev", res_model="hr.expense", res_id=7,
            filename="kvitto.jpg", data_base64="AAAA", mimetype="image/jpeg", set_as_main=True,
        )
        assert result["main_attachment"] is True
        write = next(c for c in patched_client.calls if c[1] == "write")
        assert write[0] == "hr.expense" and write[2] == [[7], {"message_main_attachment_id": 778}]

    @pytest.mark.parametrize(
        "res_model",
        ["res.users", "res.partner", "ir.config_parameter", "account.move.line", "mail.message",
         "hr.employee", "project.project", "", "Account.Move"],
    )
    def test_other_models_are_refused_before_odoo(self, patched_client, res_model):
        # set_as_main writes the target record, so a free model name would be a
        # generic write on message_main_attachment_id.
        with pytest.raises(AccessDenied, match="can only be uploaded"):
            write_safe.odoo_upload_attachment(
                instance="dev", res_model=res_model, res_id=1,
                filename="x.pdf", data_base64="AAAA", set_as_main=True,
            )
        assert patched_client.calls == []

    @pytest.mark.parametrize(
        "res_model", ["account.move", "account.payment", "hr.expense", "calendar.event"],
    )
    def test_listed_models_are_accepted(self, patched_client, res_model):
        patched_client.state = {"ir.attachment": {"create": 9}}
        result = write_safe.odoo_upload_attachment(
            instance="dev", res_model=res_model, res_id=1, filename="x.pdf", data_base64="AAAA",
        )
        assert result["res_model"] == res_model

    def test_project_task_only_when_it_is_a_todo(self, patched_client):
        task = {"id": 5, "name": "t", "state": "01_in_progress", "project_id": False, "parent_id": False}
        patched_client.state = {
            "ir.attachment": {"create": 9},
            "project.task": {"get_todo_views_id": [], "read": [task], "write": True},
        }
        write_safe.odoo_upload_attachment(
            instance="dev", res_model="project.task", res_id=5, filename="x.pdf", data_base64="AAAA",
        )
        assert [c for c in patched_client.calls if c[0] == "ir.attachment" and c[1] == "create"]

        patched_client.calls.clear()
        patched_client.state["project.task"]["read"] = [{**task, "project_id": [3, "Renovation"]}]
        with pytest.raises(ValidationError, match="only handle"):
            write_safe.odoo_upload_attachment(
                instance="dev", res_model="project.task", res_id=5, filename="x.pdf", data_base64="AAAA",
                set_as_main=True,
            )
        assert not [c for c in patched_client.calls if c[0] == "ir.attachment"]
        assert not [c for c in patched_client.calls if c[1] == "write"]

    @pytest.mark.parametrize("res_model", ["calendar.event", "hr.expense"])
    def test_set_as_main_refused_without_the_field_and_nothing_uploaded(self, patched_client, res_model):
        # Odoo 19: calendar.event and project.task lack the main-attachment mixin.
        patched_client.state = {"ir.attachment": {"create": 9}, res_model: {"fields_get": {}}}
        with pytest.raises(ValidationError, match="no main attachment"):
            write_safe.odoo_upload_attachment(
                instance="dev", res_model=res_model, res_id=1, filename="x.pdf", data_base64="AAAA",
                set_as_main=True,
            )
        assert not [c for c in patched_client.calls if c[0] == "ir.attachment"]
