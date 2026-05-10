"""Critical write tools — promote drafts to posted state, register payments.

Every tool here:

- Requires `confirm=True` in the payload. Without it the tool returns a
  preview/dry-run summary so the LLM (and the human reading the
  transcript) can sanity-check the proposed action before authorizing.
- Runs the post-time validator chain (`Registry.run_post`).
- Audit-logs the call. When `idempotency_key` is supplied the tool
  short-circuits if a successful prior call exists, returning the
  recorded summary instead of double-acting.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call, find_previous_success
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance
from odoo_mcp.validators import (
    MovePostPayload,
    ValidationError,
    get_registry,
)


def _summarize_move(client: Any, move_id: int) -> dict[str, Any]:
    """Compact summary used for both preview (dry-run) and post-result."""
    moves = client.execute_kw(
        "account.move", "read", [int(move_id)],
        {"fields": ["id", "name", "ref", "date", "state", "move_type",
                    "amount_total", "amount_residual", "currency_id",
                    "partner_id", "line_ids"]},
    )
    if not moves:
        raise ValidationError(f"Move {move_id} not found")
    move = moves[0]
    line_count = len(move.get("line_ids") or [])
    return {
        "move_id": int(move["id"]),
        "name": move.get("name"),
        "ref": move.get("ref"),
        "date": move.get("date"),
        "state": move.get("state"),
        "move_type": move.get("move_type"),
        "amount_total": move.get("amount_total"),
        "line_count": line_count,
    }


@mcp.tool()
def odoo_post_journal_entry(
    instance: Instance,
    move_id: int,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Promote a draft `account.move` to `posted` state.

    This is a *critical* write — once posted, the move's lines, tax tags,
    and most fields become read-only per Odoo's audit-trail rules. The
    typical flow is therefore:

        1. Create or import a draft (via `odoo_create_journal_entry_draft`
           or by importing a CSV in the Odoo UI).
        2. Inspect the draft (`odoo_get_invoice`).
        3. Call this tool with `confirm=False` to get a preview.
        4. Re-call with `confirm=True` to actually post.

    Args:
        instance: "prod" or "dev"
        move_id: account.move ID to post
        confirm: must be True to actually post; False returns a dry-run
            preview describing what would happen
        idempotency_key: optional caller-provided string. If a successful
            audit row already exists with this key on this instance, the
            tool returns the prior summary instead of posting again.

    Returns:
        - When confirm=False: `{preview: True, ...summary, validators_passed: bool}`
        - When confirm=True: `{posted: True, ...summary, replayed?: bool}`

    Raises:
        ValidationError if any post-time validator rejects the move
        (state != draft, unbalanced, etc.).
    """
    client = get_client(instance)

    # Idempotency short-circuit (only meaningful for confirm=True)
    if confirm and idempotency_key:
        prior = find_previous_success(idempotency_key)
        if prior:
            return {
                "posted": True,
                "replayed": True,
                "previous_call_at": prior["ts"].isoformat()
                    if hasattr(prior["ts"], "isoformat") else str(prior["ts"]),
                "previous_summary": prior["response_summary"],
            }

    # Always run post-time validators — they describe the same state on
    # both the dry-run and the real run, so users see the same go/no-go
    # signal in their preview.
    payload = MovePostPayload(instance=instance, move_id=int(move_id))
    summary = _summarize_move(client, int(move_id))

    if not confirm:
        # Dry-run: surface validator results without raising on success
        # (we still raise on failure so users see the blocking issue).
        get_registry().run_post(payload, client)
        return {
            "preview": True,
            "validators_passed": True,
            **summary,
            "next_step": (
                "Call again with confirm=True to actually post this move. "
                "Optionally pass idempotency_key for replay safety."
            ),
        }

    # Real post path
    audit_params = {
        "move_id": int(move_id),
        "name": summary.get("name"),
        "ref": summary.get("ref"),
        "amount_total": summary.get("amount_total"),
        "confirm": True,
    }
    with audit_call(
        tool="odoo_post_journal_entry",
        instance=instance,
        params=audit_params,
        idempotency_key=idempotency_key,
    ) as ctx:
        get_registry().run_post(payload, client)
        client.execute_kw(
            "account.move", "action_post", [[int(move_id)]],
        )
        # Re-read so the response reflects the new state/name
        final = _summarize_move(client, int(move_id))
        ctx.summary = (
            f"posted move id={move_id} name={final.get('name')} "
            f"amount={final.get('amount_total')}"
        )
        return {
            "posted": True,
            "replayed": False,
            **final,
        }


@mcp.tool()
def odoo_register_payment(
    instance: Instance,
    move_id: int,
    journal_code: str,
    amount: float,
    payment_date: str | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Register a payment against a posted invoice.

    Uses Odoo's `account.payment.register` wizard so the payment row is
    created and reconciled with the invoice the same way the UI does it.

    Args:
        instance: "prod" or "dev"
        move_id: posted account.move ID (vendor bill or customer invoice)
        journal_code: short code of the bank/cash journal (e.g. "BNK1")
        amount: payment amount in the invoice's currency
        payment_date: YYYY-MM-DD; defaults to the invoice date
        confirm: must be True to actually create+reconcile the payment
        idempotency_key: optional, see `odoo_post_journal_entry`

    Returns:
        - confirm=False: `{preview: True, ...invoice_summary, journal_id, amount}`
        - confirm=True: `{registered: True, payment_move_ids, ...}`
    """
    client = get_client(instance)

    if confirm and idempotency_key:
        prior = find_previous_success(idempotency_key)
        if prior:
            return {
                "registered": True,
                "replayed": True,
                "previous_summary": prior["response_summary"],
            }

    invoice = _summarize_move(client, int(move_id))
    if invoice["state"] != "posted":
        raise ValidationError(
            f"Move {move_id} is in state '{invoice['state']}'; can only "
            f"register payment against posted moves."
        )

    journals = client.execute_kw(
        "account.journal", "search_read",
        [[("code", "=", journal_code), ("type", "in", ["bank", "cash"])]],
        {"fields": ["id", "code", "name", "type"], "limit": 1},
    )
    if not journals:
        raise ValidationError(
            f"No bank/cash journal with code '{journal_code}' on {instance}"
        )
    journal = journals[0]

    if not confirm:
        return {
            "preview": True,
            "validators_passed": True,
            **invoice,
            "journal_id": int(journal["id"]),
            "journal_code": journal["code"],
            "journal_type": journal["type"],
            "amount": float(amount),
            "payment_date": payment_date or invoice.get("date"),
            "next_step": (
                "Call again with confirm=True to create and reconcile the "
                "payment. Pass idempotency_key for replay safety."
            ),
        }

    audit_params = {
        "move_id": int(move_id),
        "journal_code": journal_code,
        "amount": float(amount),
        "payment_date": payment_date,
        "confirm": True,
    }
    with audit_call(
        tool="odoo_register_payment",
        instance=instance,
        params=audit_params,
        idempotency_key=idempotency_key,
    ) as ctx:
        wizard_vals: dict[str, Any] = {
            "journal_id": int(journal["id"]),
            "amount": float(amount),
            "group_payment": False,
        }
        if payment_date:
            wizard_vals["payment_date"] = payment_date

        wizard_id = client.execute_kw(
            "account.payment.register",
            "create",
            [wizard_vals],
            {
                "context": {
                    "active_model": "account.move",
                    "active_ids": [int(move_id)],
                    "active_id": int(move_id),
                }
            },
        )
        result = client.execute_kw(
            "account.payment.register",
            "action_create_payments",
            [[int(wizard_id)]],
        )

        # `action_create_payments` typically returns an action dict that
        # references the newly created account.payment record(s). Be
        # defensive about its shape across Odoo versions.
        payment_ids: list[int] = []
        if isinstance(result, dict):
            domain = result.get("domain") or []
            res_id = result.get("res_id")
            if res_id:
                payment_ids.append(int(res_id))
            else:
                for clause in domain:
                    if (
                        isinstance(clause, list | tuple)
                        and len(clause) == 3
                        and clause[0] == "id"
                        and clause[1] == "in"
                    ):
                        payment_ids.extend(int(x) for x in clause[2])
                        break

        ctx.summary = (
            f"registered payment of {amount} on move {move_id} via "
            f"journal {journal_code}; payment_ids={payment_ids}"
        )
        return {
            "registered": True,
            "replayed": False,
            "move_id": int(move_id),
            "journal_code": journal_code,
            "amount": float(amount),
            "payment_ids": payment_ids,
        }
