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


@dataclass
class MovePostPayload:
    """Payload for posting an existing draft account.move.

    Used by post-time validators that need to inspect the move as it
    currently exists in Odoo (account_code on each line, balance, etc.).
    `move_id` is the only required input from the tool caller — the
    validator fetches the rest via `client`.
    """

    instance: str
    move_id: int


@dataclass
class InvoiceLinePayload:
    name: str
    price_unit: float
    quantity: float = 1.0
    account_code: str | None = None
    tax_names: list[str] = field(default_factory=list)
    product_id: int | None = None


@dataclass
class InvoicePayload:
    instance: str
    move_type: str
    partner_id: int
    lines: list[InvoiceLinePayload]
    invoice_date: str | None = None
    ref: str | None = None
    journal_code: str | None = None


class Validator(Protocol):
    name: str

    def __call__(self, payload: JournalEntryPayload, client: Any) -> None:
        """Run the validator. Raise ValidationError on failure."""


class PostValidator(Protocol):
    """Validators that run before posting an existing draft move.

    Distinct from `Validator` because they take a different payload shape
    (a move_id to inspect, not a fresh entry to be created).
    """

    name: str

    def __call__(self, payload: MovePostPayload, client: Any) -> None:
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


# ---- Post-time validators (run before promoting draft → posted) ----------


class PostBalanceValidator:
    """Re-check that the move balances right before posting.

    Belt-and-braces: Odoo enforces balance on post anyway, but failing
    here gives a clearer error than the SQL-level integrity violation.
    """

    name = "post_balance"

    def __call__(self, payload: MovePostPayload, client: Any) -> None:
        lines = client.execute_kw(
            "account.move.line", "search_read",
            [[("move_id", "=", payload.move_id)]],
            {"fields": ["debit", "credit"]},
        )
        if not lines:
            raise ValidationError(
                f"Move {payload.move_id} has no lines on {payload.instance}"
            )
        debit = sum((Decimal(str(line["debit"])) for line in lines), Decimal(0))
        credit = sum((Decimal(str(line["credit"])) for line in lines), Decimal(0))
        if debit.quantize(Decimal("0.01")) != credit.quantize(Decimal("0.01")):
            raise ValidationError(
                f"Move {payload.move_id} is not balanced: "
                f"debit={debit:.2f} != credit={credit:.2f}. "
                f"Difference: {(debit - credit):.2f}"
            )


class PostStateValidator:
    """Block posting a move that's not currently in `draft` state.

    Avoids a confusing 'no-op' if the user accidentally double-posts or
    runs the tool against a cancelled move.
    """

    name = "post_state"

    def __call__(self, payload: MovePostPayload, client: Any) -> None:
        moves = client.execute_kw(
            "account.move", "read", [payload.move_id], {"fields": ["state"]},
        )
        if not moves:
            raise ValidationError(
                f"Move {payload.move_id} not found on {payload.instance}"
            )
        state = moves[0]["state"]
        if state != "draft":
            raise ValidationError(
                f"Move {payload.move_id} is in state '{state}', cannot post. "
                f"Only `draft` moves can be promoted."
            )


# ---- Invoice validators (run before creating a draft invoice) ------------

_SALE_MOVE_TYPES = {"out_invoice", "out_refund"}
_PURCHASE_MOVE_TYPES = {"in_invoice", "in_refund"}


class InvoiceValidator(Protocol):
    name: str

    def __call__(self, payload: InvoicePayload, client: Any) -> None:
        """Run the validator. Raise ValidationError on failure."""


class InvoiceStructureValidator:
    """Structural sanity for a draft invoice before it reaches Odoo.

    Tax *direction* (sale vs purchase) correctness is enforced where the tax
    ids are actually resolved (`write_safe._resolve_invoice_tax_ids`), so a
    wrong-direction tax can never silently produce incorrect VAT that would
    flow into the momsrapport.
    """

    name = "invoice_structure"

    def __call__(self, payload: InvoicePayload, client: Any) -> None:
        if payload.move_type not in _SALE_MOVE_TYPES | _PURCHASE_MOVE_TYPES:
            raise ValidationError(
                f"Unsupported move_type '{payload.move_type}'. Use one of: "
                "out_invoice, in_invoice, out_refund, in_refund."
            )
        if not payload.lines:
            raise ValidationError("Invoice must have at least one line.")
        for i, line in enumerate(payload.lines):
            if not line.name:
                raise ValidationError(f"Invoice line {i}: name (description) is required.")
            if line.quantity == 0:
                raise ValidationError(
                    f"Invoice line {i} ({line.name}): quantity must be non-zero."
                )


# ---- Registry -------------------------------------------------------------


@dataclass
class Registry:
    validators: list[Validator] = field(default_factory=list)
    post_validators: list[PostValidator] = field(default_factory=list)
    invoice_validators: list[InvoiceValidator] = field(default_factory=list)

    def register(self, validator: Validator) -> None:
        self.validators.append(validator)

    def register_post(self, validator: PostValidator) -> None:
        self.post_validators.append(validator)

    def register_invoice(self, validator: InvoiceValidator) -> None:
        self.invoice_validators.append(validator)

    def run(self, payload: JournalEntryPayload, client: Any) -> None:
        for v in self.validators:
            v(payload, client)

    def run_post(self, payload: MovePostPayload, client: Any) -> None:
        for v in self.post_validators:
            v(payload, client)

    def run_invoice(self, payload: InvoicePayload, client: Any) -> None:
        for v in self.invoice_validators:
            v(payload, client)


_registry: Registry | None = None


def get_registry() -> Registry:
    global _registry
    if _registry is None:
        _registry = Registry()
        _registry.register(BalanceValidator())
        _registry.register(AccountsExistValidator())
        _registry.register(TaxTagsExistValidator())
        _registry.register_post(PostStateValidator())
        _registry.register_post(PostBalanceValidator())
        _registry.register_invoice(InvoiceStructureValidator())
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
