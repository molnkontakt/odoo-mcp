"""Tests for write_safe tools using MockClient."""

from __future__ import annotations

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.tools import write_safe
from odoo_mcp.validators import ValidationError


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    """Replace get_client with a function returning the mock."""
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(write_safe, "get_client", lambda inst: mock_client)
    return mock_client


@pytest.fixture
def loaded_state():
    """Realistic Odoo response state for a happy-path entry creation."""
    created_id = 4242
    return {
        "account.journal": {
            "search_read": [{"id": 1, "code": "MISC", "name": "Misc"}],
        },
        "account.account": {
            "search_read": [
                {"id": 153, "code": "2614"},
                {"id": 173, "code": "2645"},
                {"id": 253, "code": "4535"},
                {"id": 1242, "code": "6231"},
            ],
        },
        "account.account.tag": {
            "search_read": [
                {"id": 53, "name": "se_21"},
                {"id": 57, "name": "se_30"},
                {"id": 72, "name": "se_48"},
            ],
        },
        "account.move": {
            "create": created_id,
            "read": [{"id": created_id, "name": "/", "state": "draft"}],
            "write": True,
        },
        "account.move.line": {
            "read": [],
            "write": True,
        },
        "res.partner": {
            "read": [{"id": 7, "name": "Acme AB"}],
        },
    }, created_id


class TestCreateJournalEntryDraft:
    def test_happy_path(self, patched_client, loaded_state):
        state, created_id = loaded_state
        patched_client.state = state

        result = write_safe.odoo_create_journal_entry_draft(
            instance="dev",
            date="2026-01-01",
            ref="Test entry",
            lines=[
                {"account_code": "4535", "credit": 395.76, "tax_tag_codes": ["se_21"]},
                {"account_code": "2614", "debit": 98.94, "tax_tag_codes": ["se_30"]},
                {"account_code": "2645", "credit": 98.94, "tax_tag_codes": ["se_48"]},
                {"account_code": "6231", "debit": 395.76},
            ],
        )
        assert result["move_id"] == created_id
        assert result["state"] == "draft"
        assert result["line_count"] == 4

        # Verify a create call was issued with line_ids of length 4
        create_calls = [c for c in patched_client.calls if c[1] == "create" and c[0] == "account.move"]
        assert len(create_calls) == 1
        move_vals = create_calls[0][2][0]
        assert len(move_vals["line_ids"]) == 4

    def test_unbalanced_rejected(self, patched_client, loaded_state):
        state, _ = loaded_state
        patched_client.state = state

        with pytest.raises(ValidationError, match="not balanced"):
            write_safe.odoo_create_journal_entry_draft(
                instance="dev",
                date="2026-01-01",
                lines=[
                    {"account_code": "4535", "credit": 100},
                    {"account_code": "6231", "debit": 99},
                ],
            )

    def test_unknown_account_rejected(self, patched_client, loaded_state):
        state, _ = loaded_state
        patched_client.state = state

        with pytest.raises(ValidationError, match="9999"):
            write_safe.odoo_create_journal_entry_draft(
                instance="dev",
                date="2026-01-01",
                lines=[
                    {"account_code": "9999", "credit": 100},
                    {"account_code": "6231", "debit": 100},
                ],
            )

    def test_unknown_tag_rejected(self, patched_client, loaded_state):
        state, _ = loaded_state
        patched_client.state = state

        with pytest.raises(ValidationError, match="se_99"):
            write_safe.odoo_create_journal_entry_draft(
                instance="dev",
                date="2026-01-01",
                lines=[
                    {"account_code": "4535", "credit": 100, "tax_tag_codes": ["se_99"]},
                    {"account_code": "6231", "debit": 100},
                ],
            )

    def test_empty_lines_rejected(self, patched_client):
        with pytest.raises(ValidationError, match="must not be empty"):
            write_safe.odoo_create_journal_entry_draft(
                instance="dev",
                date="2026-01-01",
                lines=[],
            )


class TestSetPartner:
    def test_happy_path(self, patched_client, loaded_state):
        state, _ = loaded_state
        # Override account.move.read to return draft state
        state["account.move"]["read"] = [{"state": "draft"}]
        patched_client.state = state

        result = write_safe.odoo_set_partner(
            instance="dev", move_id=100, partner_id=7,
        )
        assert result["partner_id"] == 7
        assert result["partner_name"] == "Acme AB"

    def test_rejects_posted_move(self, patched_client, loaded_state):
        state, _ = loaded_state
        state["account.move"]["read"] = [{"state": "posted"}]
        patched_client.state = state

        with pytest.raises(ValidationError, match="not a draft"):
            write_safe.odoo_set_partner(
                instance="dev", move_id=100, partner_id=7,
            )


class TestAddTaxTags:
    def test_happy_path_replace(self, patched_client, loaded_state):
        state, _ = loaded_state
        state["account.move.line"]["read"] = [
            {"id": 500, "move_id": [42], "parent_state": "draft", "tax_tag_ids": []}
        ]
        patched_client.state = state

        result = write_safe.odoo_add_tax_tags(
            instance="dev", line_id=500, tag_codes=["se_30", "se_48"], replace=True,
        )
        assert result["line_id"] == 500
        assert result["applied_tags"] == ["se_30", "se_48"]

    def test_rejects_posted_parent(self, patched_client, loaded_state):
        state, _ = loaded_state
        state["account.move.line"]["read"] = [
            {"id": 500, "parent_state": "posted", "tax_tag_ids": []}
        ]
        patched_client.state = state

        with pytest.raises(ValidationError, match="locked"):
            write_safe.odoo_add_tax_tags(
                instance="dev", line_id=500, tag_codes=["se_30"],
            )
