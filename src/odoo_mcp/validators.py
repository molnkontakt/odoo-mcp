"""Validators that run against journal entry payloads before write tools accept them.

The framework is intentionally simple: each validator is a callable that
takes the proposed payload and raises `ValidationError` if it rejects.
Validators can be registered globally or chosen per call site.

This module ships with three built-ins, all always on:
- `BalanceValidator`: debit total must equal credit total
- `AccountsExistValidator`: every referenced account_code resolves
- `TaxTagsExistValidator`: every tag_code resolves on the instance

Domain-specific validators (e.g. Swedish VAT one-sided reverse charge,
period locks) can be loaded from `MCP_VALIDATORS_PATH` — a `:`-separated
list of importable module paths whose top-level `register(registry)`
function is called at startup.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol


class ValidationError(ValueError):
    """Raised when a payload fails validation. The message becomes the tool error."""


@dataclass
class JournalLinePayload:
    account_code: str
    debit: float = 0.0
    credit: float = 0.0
    name: str | None = None
    tax_tag_codes: list[str] = field(default_factory=list)
    partner_id: int | None = None


@dataclass
class JournalEntryPayload:
    instance: str
    date: str
    ref: str | None
    journal_code: str | None
    lines: list[JournalLinePayload]


class Validator(Protocol):
    name: str

    def __call__(self, payload: JournalEntryPayload, client: Any) -> None:
        """Run the validator. Raise ValidationError on failure."""


# ---- Built-in validators -------------------------------------------------


class BalanceValidator:
    name = "balance"

    def __call__(self, payload: JournalEntryPayload, client: Any) -> None:
        # Use Decimal so 0.07-cent rounding doesn't slip through float math
        debit = sum((Decimal(str(line.debit)) for line in payload.lines), Decimal(0))
        credit = sum((Decimal(str(line.credit)) for line in payload.lines), Decimal(0))
        if debit.quantize(Decimal("0.01")) != credit.quantize(Decimal("0.01")):
            raise ValidationError(
                f"Journal entry is not balanced: debit={debit:.2f} != credit={credit:.2f}. "
                f"Difference: {(debit - credit):.2f}"
            )


class AccountsExistValidator:
    name = "accounts_exist"

    def __call__(self, payload: JournalEntryPayload, client: Any) -> None:
        codes = sorted({line.account_code for line in payload.lines})
        if not codes:
            return
        accs = client.execute_kw(
            "account.account", "search_read",
            [[("code", "in", codes)]],
            {"fields": ["code"]},
        )
        found = {a["code"] for a in accs}
        missing = [c for c in codes if c not in found]
        if missing:
            raise ValidationError(
                f"Account code(s) not found on {payload.instance}: {', '.join(missing)}"
            )


class TaxTagsExistValidator:
    name = "tax_tags_exist"

    def __call__(self, payload: JournalEntryPayload, client: Any) -> None:
        all_codes = sorted({
            tag for line in payload.lines for tag in line.tax_tag_codes
        })
        if not all_codes:
            return
        # account.account.tag.name is a translated Char → search via en_US
        tags = client.execute_kw(
            "account.account.tag", "search_read",
            [[("name", "in", all_codes), ("applicability", "=", "taxes")]],
            {"fields": ["name"]},
        )
        found = {t["name"] for t in tags}
        missing = [c for c in all_codes if c not in found]
        if missing:
            raise ValidationError(
                f"Tax tag code(s) not found on {payload.instance}: {', '.join(missing)}"
            )


# ---- Registry -------------------------------------------------------------


@dataclass
class Registry:
    validators: list[Validator] = field(default_factory=list)

    def register(self, validator: Validator) -> None:
        self.validators.append(validator)

    def run(self, payload: JournalEntryPayload, client: Any) -> None:
        for v in self.validators:
            v(payload, client)


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
        _registry.register(BalanceValidator())
        _registry.register(AccountsExistValidator())
        _registry.register(TaxTagsExistValidator())
        _load_external(_registry)
    return _registry


def _load_external(registry: Registry) -> None:
    """Load validators from MCP_VALIDATORS_PATH (colon-separated module paths).

    Each module must expose `register(registry)` that adds zero or more
    validators to the registry.
    """
    path = os.environ.get("MCP_VALIDATORS_PATH")
    if not path:
        return
    for module_path in path.split(":"):
        module_path = module_path.strip()
        if not module_path:
            continue
        try:
            module = importlib.import_module(module_path)
            register_fn: Callable[[Registry], None] | None = getattr(
                module, "register", None
            )
            if register_fn:
                register_fn(registry)
        except Exception as e:
            # Don't crash startup on a misconfigured plugin; log via stderr.
            import logging
            logging.getLogger(__name__).warning(
                "Failed to load validator plugin %s: %s", module_path, e
            )
