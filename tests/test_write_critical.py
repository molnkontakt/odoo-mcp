"""Tests for write_critical tools — preview path, confirm path, validators,
idempotency."""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from odoo_mcp import audit
from odoo_mcp import client as client_module
from odoo_mcp.tools import write_critical
from odoo_mcp.validators import ValidationError


@pytest.fixture
def patched(mock_client, monkeypatch):
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(write_critical, "get_client", lambda inst: mock_client)
    # Disable audit DB lookups during tests
    monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
    monkeypatch.setattr(audit, "_logger_instance", None)
    return mock_client


def _draft_invoice(state: str = "draft", line_count: int = 4) -> dict[str, Any]:
    return {
        "id": 100,
        "name": "/" if state == "draft" else "BILL/2026/0001",
        "ref": "Test ref",
        "date": "2026-01-01",
        "state": state,
        "move_type": "in_invoice",
        "amount_total": 494.70,
        "amount_residual": 494.70,
        "currency_id": [1, "SEK"],
        "partner_id": [7, "Acme AB"],
        "line_ids": list(range(1, line_count + 1)),
    }


def _balanced_lines() -> list[dict[str, Any]]:
    return [
        {"debit": 100.0, "credit": 0.0},
        {"debit": 0.0, "credit": 100.0},
    ]


class TestPostJournalEntry:
    def test_preview_returns_summary_without_posting(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice()]},
            "account.move.line": {"search_read": _balanced_lines()},
        }
        result = write_critical.odoo_post_journal_entry(
            instance="dev", move_id=100, confirm=False,
        )
        assert result["preview"] is True
        assert result["validators_passed"] is True
        assert result["state"] == "draft"
        # No action_post call when previewing
        assert not any(c[1] == "action_post" for c in patched.calls)

    def test_confirm_calls_action_post(self, patched):
        # Reads in order:
        #  1. _summarize_move (preview-of-draft before audit_call)
        #  2. PostStateValidator (still draft)
        #  3. _summarize_move after action_post (now posted)
        states = iter([
            [_draft_invoice("draft")],
            [_draft_invoice("draft")],
            [_draft_invoice("posted")],
        ])
        patched.state = {
            "account.move": {
                "read": lambda args, kwargs: next(states),
            },
            "account.move.line": {"search_read": _balanced_lines()},
        }
        result = write_critical.odoo_post_journal_entry(
            instance="dev", move_id=100, confirm=True,
        )
        assert result["posted"] is True
        assert result["replayed"] is False
        post_calls = [c for c in patched.calls if c[1] == "action_post"]
        assert len(post_calls) == 1
        assert post_calls[0][2] == [[100]]

    def test_rejects_already_posted(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("posted")]},
            "account.move.line": {"search_read": _balanced_lines()},
        }
        with pytest.raises(ValidationError, match="state 'posted'"):
            write_critical.odoo_post_journal_entry(
                instance="dev", move_id=100, confirm=True,
            )

    def test_rejects_unbalanced_move(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("draft")]},
            "account.move.line": {
                "search_read": [
                    {"debit": 100.0, "credit": 0.0},
                    {"debit": 0.0, "credit": 50.0},
                ],
            },
        }
        with pytest.raises(ValidationError, match="not balanced"):
            write_critical.odoo_post_journal_entry(
                instance="dev", move_id=100, confirm=True,
            )

    def test_missing_move_raises(self, patched):
        patched.state = {
            "account.move": {"read": []},
        }
        with pytest.raises(ValidationError, match="not found"):
            write_critical.odoo_post_journal_entry(
                instance="dev", move_id=999, confirm=False,
            )

    def test_idempotency_replay_short_circuits(self, patched, monkeypatch):
        """When a prior successful call exists with the same key, return the
        cached summary instead of doing the write again."""
        from datetime import datetime

        prior = {
            "id": 42,
            "ts": datetime(2026, 5, 9, 12, 0, tzinfo=UTC),
            "tool": "odoo_post_journal_entry",
            "response_summary": "posted move id=100 (cached)",
            "params": {},
        }
        monkeypatch.setattr(
            write_critical, "find_previous_success",
            lambda **kwargs: prior,
        )
        result = write_critical.odoo_post_journal_entry(
            instance="dev", move_id=100, confirm=True,
            idempotency_key="abc-123",
        )
        assert result["replayed"] is True
        assert result["previous_summary"] == "posted move id=100 (cached)"
        # No action_post call when replayed
        assert not any(c[1] == "action_post" for c in patched.calls)

    def test_idempotency_only_active_with_confirm(self, patched, monkeypatch):
        """A preview call (confirm=False) should never short-circuit even
        if a prior success exists; previews are read-only and informational."""
        called = []
        monkeypatch.setattr(
            write_critical, "find_previous_success",
            lambda **kwargs: called.append(kwargs) or None,
        )
        patched.state = {
            "account.move": {"read": [_draft_invoice("draft")]},
            "account.move.line": {"search_read": _balanced_lines()},
        }
        result = write_critical.odoo_post_journal_entry(
            instance="dev", move_id=100, confirm=False,
            idempotency_key="abc-123",
        )
        assert result["preview"] is True
        # Lookup should not happen on preview
        assert called == []


class TestRegisterPayment:
    def test_preview(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("posted")]},
            "account.journal": {
                "search_read": [{"id": 12, "code": "BNK1", "name": "Bank", "type": "bank"}],
            },
        }
        result = write_critical.odoo_register_payment(
            instance="dev", move_id=100, journal_code="BNK1",
            amount=494.70, confirm=False,
        )
        assert result["preview"] is True
        assert result["journal_code"] == "BNK1"
        assert result["amount"] == 494.70
        assert not any(c[0] == "account.payment.register" for c in patched.calls)

    def test_confirm_creates_payment(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("posted")]},
            "account.journal": {
                "search_read": [{"id": 12, "code": "BNK1", "name": "Bank", "type": "bank"}],
            },
            "account.payment.register": {
                "create": 555,
                "action_create_payments": {"res_id": 9001},
            },
        }
        result = write_critical.odoo_register_payment(
            instance="dev", move_id=100, journal_code="BNK1",
            amount=494.70, payment_date="2026-01-15", confirm=True,
        )
        assert result["registered"] is True
        assert result["payment_ids"] == [9001]

    def test_rejects_payment_on_draft_invoice(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("draft")]},
        }
        with pytest.raises(ValidationError, match="state 'draft'"):
            write_critical.odoo_register_payment(
                instance="dev", move_id=100, journal_code="BNK1",
                amount=494.70, confirm=False,
            )

    def test_rejects_unknown_journal(self, patched):
        patched.state = {
            "account.move": {"read": [_draft_invoice("posted")]},
            "account.journal": {"search_read": []},
        }
        with pytest.raises(ValidationError, match="No bank/cash journal"):
            write_critical.odoo_register_payment(
                instance="dev", move_id=100, journal_code="ZZZZ",
                amount=494.70, confirm=False,
            )
