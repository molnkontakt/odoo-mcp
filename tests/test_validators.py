"""Unit tests for validators."""

from __future__ import annotations

import pytest

from odoo_mcp.validators import (
    AccountsExistValidator,
    BalanceValidator,
    JournalEntryPayload,
    JournalLinePayload,
    Registry,
    TaxTagsExistValidator,
    ValidationError,
)


def _payload(lines: list[JournalLinePayload]) -> JournalEntryPayload:
    return JournalEntryPayload(
        instance="dev", date="2026-05-09", ref="test", journal_code=None, lines=lines,
    )


class TestBalanceValidator:
    def test_balanced_passes(self, mock_client):
        payload = _payload([
            JournalLinePayload("2611", debit=0, credit=100),
            JournalLinePayload("4000", debit=100, credit=0),
        ])
        BalanceValidator()(payload, mock_client)  # no raise

    def test_unbalanced_raises(self, mock_client):
        payload = _payload([
            JournalLinePayload("2611", debit=0, credit=100),
            JournalLinePayload("4000", debit=99, credit=0),
        ])
        with pytest.raises(ValidationError, match="not balanced"):
            BalanceValidator()(payload, mock_client)

    def test_decimal_precision_balanced(self, mock_client):
        # Floats that look unbalanced but quantize to equal
        payload = _payload([
            JournalLinePayload("a", debit=110763.78, credit=0),
            JournalLinePayload("b", debit=0, credit=7433.71),
            JournalLinePayload("c", debit=0, credit=103330.00),
            JournalLinePayload("d", debit=0, credit=0.07),
        ])
        BalanceValidator()(payload, mock_client)


class TestAccountsExistValidator:
    def test_all_present(self, mock_client):
        mock_client.state = {
            "account.account": {
                "search_read": [{"code": "2611"}, {"code": "4000"}]
            }
        }
        payload = _payload([
            JournalLinePayload("2611", credit=100),
            JournalLinePayload("4000", debit=100),
        ])
        AccountsExistValidator()(payload, mock_client)

    def test_missing_raises(self, mock_client):
        mock_client.state = {
            "account.account": {"search_read": [{"code": "2611"}]}
        }
        payload = _payload([
            JournalLinePayload("2611", credit=100),
            JournalLinePayload("9999", debit=100),
        ])
        with pytest.raises(ValidationError, match="9999"):
            AccountsExistValidator()(payload, mock_client)


class TestTaxTagsExistValidator:
    def test_no_tags_passes(self, mock_client):
        payload = _payload([JournalLinePayload("2611", credit=100)])
        TaxTagsExistValidator()(payload, mock_client)

    def test_all_present(self, mock_client):
        mock_client.state = {
            "account.account.tag": {
                "search_read": [{"name": "se_30"}, {"name": "se_48"}]
            }
        }
        payload = _payload([
            JournalLinePayload("2611", credit=100, tax_tag_codes=["se_30"]),
            JournalLinePayload("4000", debit=100, tax_tag_codes=["se_48"]),
        ])
        TaxTagsExistValidator()(payload, mock_client)

    def test_missing_raises(self, mock_client):
        mock_client.state = {
            "account.account.tag": {"search_read": [{"name": "se_30"}]}
        }
        payload = _payload([
            JournalLinePayload("2611", credit=100, tax_tag_codes=["se_99"]),
        ])
        with pytest.raises(ValidationError, match="se_99"):
            TaxTagsExistValidator()(payload, mock_client)


class TestRegistry:
    def test_runs_all_in_order(self, mock_client):
        seen: list[str] = []

        class Recorder:
            def __init__(self, name: str):
                self.name = name

            def __call__(self, payload, client):
                seen.append(self.name)

        reg = Registry()
        reg.register(Recorder("a"))
        reg.register(Recorder("b"))
        reg.run(_payload([]), mock_client)
        assert seen == ["a", "b"]
