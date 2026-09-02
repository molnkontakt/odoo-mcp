"""Critical write tools — promote drafts to posted state, register payments.

Every tool here:

- Requires `confirm=True` in the payload. Without it the tool returns a
  preview/dry-run summary so the LLM (and the human reading the
  transcript) can sanity-check the proposed action before authorizing.
- Runs tool-specific pre-checks before doing the write (the post-time
  validator chain on `odoo_post_journal_entry`; existence + state checks
  on `odoo_register_payment`).
- Audit-logs the *confirmed* call. Preview calls (`confirm=False`) are
  not audit-logged because they don't change state.
- When `idempotency_key` is supplied (only meaningful for `confirm=True`),
  the tool short-circuits if a successful prior call exists for the same
  `(instance, tool, key)` triple, returning the recorded summary instead
  of double-acting.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call, find_previous_success, fingerprint_params
from odoo_mcp.auth import SCOPE_CRITICAL, requires_scope
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance
from odoo_mcp.validators import (
    MovePostPayload,
    ValidationError,
    get_registry,
)


def _check_replay(
    *,
    tool: str,
    instance: str,
    idempotency_key: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """Look up a prior call for this key and validate it was the same request.

    The key alone is not enough. Without comparing the parameters, reusing a
    key against a different move returns a confident `{"replayed": true}` for
    work that was never performed on that move.
    """
    prior = find_previous_success(
        idempotency_key=idempotency_key, instance=instance, tool=tool
    )
    if not prior:
        return None

    now_fp = fingerprint_params(params)
    prior_fp = prior.get("params_fingerprint")
    if prior_fp and prior_fp != now_fp:
        raise ValidationError(
            f"idempotency_key '{idempotency_key}' was already used on "
            f"{instance}/{tool} with different parameters (recorded "
            f"{prior['ts']}). Refusing to report this call as a replay of a "
            f"different request — use a fresh key."
        )

    status = prior.get("status")
    if status == "in_progress":
        raise ValidationError(
            f"idempotency_key '{idempotency_key}' has an unfinished call from "
            f"{prior['ts']} (audit row {prior['id']} is still in_progress). "
            f"The earlier attempt may have been applied in Odoo. Verify there "
            f"before retrying; do not reuse this key."
        )
    if status == "unknown":
        raise ValidationError(
            f"idempotency_key '{idempotency_key}' belongs to a call whose "
            f"outcome is unknown (audit row {prior['id']}, {prior['ts']}) — "
            f"the request may have reached Odoo. Verify in Odoo before "
            f"retrying; do not reuse this key."
        )
    return prior


def _replay_response(prior: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Build the short-circuit response for an already-performed call."""
    ts = prior.get("ts")
    return {
        "replayed": True,
        "previous_call_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        "previous_summary": prior.get("response_summary"),
        "previous_status": prior.get("status"),
        "audit_row_id": prior.get("id"),
        **extra,
    }


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
@requires_scope(SCOPE_CRITICAL)
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

    # Idempotency short-circuit (only meaningful for confirm=True). Scoped to
    # (instance, tool, key), and the caller's arguments must match — see
    # _check_replay. Deliberately before any Odoo call: a replay must not
    # depend on the move still being readable.
    request_params = {"move_id": int(move_id), "confirm": True}
    if confirm and idempotency_key:
        prior = _check_replay(
            tool="odoo_post_journal_entry",
            instance=instance,
            idempotency_key=idempotency_key,
            params=request_params,
        )
        if prior:
            return _replay_response(prior, posted=True, move_id=int(move_id))

    # Always run post-time validators — they describe the same state on
    # both the dry-run and the real run, so users see the same go/no-go
    # signal in their preview.
    payload = MovePostPayload(instance=instance, move_id=int(move_id))
    summary = _summarize_move(client, int(move_id))

    audit_params = {
        "move_id": int(move_id),
        "name": summary.get("name"),
        "ref": summary.get("ref"),
        "amount_total": summary.get("amount_total"),
        "confirm": True,
    }

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
    with audit_call(
        tool="odoo_post_journal_entry",
        instance=instance,
        params=audit_params,
        idempotency_key=idempotency_key,
        params_fingerprint=fingerprint_params(request_params),
        critical=True,
    ) as ctx:
        get_registry().run_post(payload, client)
        client.execute_kw(
            "account.move", "action_post", [[int(move_id)]],
        )
        # The move is posted from here on. Mark it before anything else can
        # fail, so a later error cannot be mistaken for "nothing happened".
        ctx.mark_committed()
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
@requires_scope(SCOPE_CRITICAL)
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

    # Replay check before any Odoo call — the fingerprint covers the caller's
    # raw arguments, so `payment_date=None` stays distinct from an explicit
    # date even though both resolve to the same effective date below.
    request_params = {
        "move_id": int(move_id),
        "journal_code": journal_code,
        "amount": float(amount),
        "payment_date": payment_date,
        "confirm": True,
    }
    if confirm and idempotency_key:
        prior = _check_replay(
            tool="odoo_register_payment",
            instance=instance,
            idempotency_key=idempotency_key,
            params=request_params,
        )
        if prior:
            return _replay_response(prior, registered=True, move_id=int(move_id))

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

    # Resolve the accounting date ONCE, so the preview cannot promise one date
    # while the write books another. Odoo's account.payment.register defaults
    # payment_date to context_today, which is *not* the invoice date — leaving
    # this to the wizard made confirm=False and confirm=True disagree on the
    # field that decides period and fiscal year.
    effective_payment_date = payment_date or invoice.get("date")

    if not confirm:
        return {
            "preview": True,
            # NOTE: no `validators_passed` key here on purpose. No validator
            # runs for payments, and reporting True would borrow the meaning
            # the key legitimately carries in odoo_post_journal_entry, where
            # it follows an actual run_post(). Absence is honest; a hardcoded
            # True is not.
            **invoice,
            "journal_id": int(journal["id"]),
            "journal_code": journal["code"],
            "journal_type": journal["type"],
            "amount": float(amount),
            "payment_date": effective_payment_date,
            "next_step": (
                "Call again with confirm=True to create and reconcile the "
                "payment. Pass idempotency_key for replay safety."
            ),
        }

    audit_params = {
        "move_id": int(move_id),
        "journal_code": journal_code,
        "amount": float(amount),
        "payment_date": effective_payment_date,
        "confirm": True,
    }

    with audit_call(
        tool="odoo_register_payment",
        instance=instance,
        params=audit_params,
        idempotency_key=idempotency_key,
        params_fingerprint=fingerprint_params(request_params),
        critical=True,
    ) as ctx:
        wizard_vals: dict[str, Any] = {
            "journal_id": int(journal["id"]),
            "amount": float(amount),
            "group_payment": False,
        }
        if effective_payment_date:
            wizard_vals["payment_date"] = effective_payment_date

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
        # Money has moved. Everything below is parsing, and parsing failures
        # must not make this look replayable.
        ctx.mark_committed()

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


def _reversal_field_names(client: Any) -> set[str]:
    """Discover which fields the local Odoo's account.move.reversal model
    accepts. Lets us stay version-agnostic (the wizard's schema has
    drifted between Odoo 16/17/18/19)."""
    fields = client.execute_kw("account.move.reversal", "fields_get", [], {})
    return set(fields.keys())


@mcp.tool()
@requires_scope(SCOPE_CRITICAL)
def odoo_reverse_move(
    instance: Instance,
    move_id: int,
    reason: str,
    journal_code: str | None = None,
    date: str | None = None,
    confirm: bool = False,
    idempotency_key: str | None = None,
    allow_additional_reversal: bool = False,
) -> dict[str, Any]:
    """Reverse a posted account.move via Odoo's `account.move.reversal` wizard.

    Creates a new move with the original lines flipped (debit↔credit) and links
    it back via `reversed_entry_id`. The original move is left untouched so the
    audit trail is preserved end-to-end (BFL 5 kap 5 § in Sweden — corrections
    must be visible alongside the originals, never overwrite them).

    IMPORTANT — the reversal is not always posted. Odoo's `refund_moves()` only
    posts and reconciles the reversal when the move's `move_type` is `entry`
    (a manual journal entry). For invoices, bills and refunds it creates the
    credit note in DRAFT and leaves the original fully open. This tool reports
    what actually happened: `reversed` is True only when every created reversal
    is posted, and `reversal_state` carries the per-move states either way.
    A draft reversal still needs `odoo_post_journal_entry` to take effect.

    Use cases:
    - Undo a smoke-test or accidental post
    - Issue a credit memo against a vendor bill
    - Reverse a period-end journal entry that turned out wrong

    Args:
        instance: "prod" or "dev"
        move_id: ID of the posted move to reverse
        reason: short human description; included on the new move's ref
        journal_code: journal short code for the reversal entry; defaults
            to the same journal as the original move
        date: YYYY-MM-DD; defaults to today (Odoo wizard default)
        confirm: must be True to actually run; False returns a dry-run
            preview describing what would happen
        idempotency_key: optional; same semantics as the other write_critical
            tools
        allow_additional_reversal: reverse even though a reversal already
            exists. Off by default — a second reversal nets the ledger back
            out while leaving two spurious verifications behind.

    Returns:
        - confirm=False: `{preview: True, original, journal_code, reason, date,
          existing_reversals}`
        - confirm=True: `{reversed: bool, reversal_state: [...], replayed: bool,
          original, reversal: [{move_id, name, state, amount_total}]}`

    Raises:
        ValidationError if the original move is not in `posted` state, or if it
        is already reversed and allow_additional_reversal is False.
    """
    client = get_client(instance)

    request_params = {
        "move_id": int(move_id),
        "reason": reason,
        "journal_code": journal_code,
        "date": date,
        "confirm": True,
    }
    if confirm and idempotency_key:
        prior = _check_replay(
            tool="odoo_reverse_move",
            instance=instance,
            idempotency_key=idempotency_key,
            params=request_params,
        )
        if prior:
            return _replay_response(prior, reversed=True, move_id=int(move_id))

    original = _summarize_move(client, int(move_id))
    if original["state"] != "posted":
        raise ValidationError(
            f"Move {move_id} is in state '{original['state']}'; only "
            f"posted moves can be reversed."
        )

    # Resolve target journal — default to the original's journal
    if journal_code:
        journals = client.execute_kw(
            "account.journal", "search_read",
            [[("code", "=", journal_code)]],
            {"fields": ["id", "code"], "limit": 1},
        )
        if not journals:
            raise ValidationError(
                f"No journal with code '{journal_code}' on {instance}"
            )
        journal_id = int(journals[0]["id"])
    else:
        # Read original's journal_id
        moves = client.execute_kw(
            "account.move", "read", [int(move_id)],
            {"fields": ["journal_id"]},
        )
        journal_id = int(moves[0]["journal_id"][0]) if moves and moves[0].get("journal_id") else None
        if journal_id is None:
            raise ValidationError(f"Move {move_id} has no journal_id")

    # Guard against reversing something that was already reversed. Without this
    # a repeated call creates a second mirror-image entry of the same size, so
    # the ledger nets out correctly while carrying two spurious verifications.
    existing = client.execute_kw(
        "account.move", "search_read",
        [[("reversed_entry_id", "=", int(move_id))]],
        {"fields": ["id", "name", "state"], "limit": 5},
    )
    if existing and not allow_additional_reversal:
        names = [e.get("name") for e in existing]
        raise ValidationError(
            f"Move {move_id} ({original.get('name')}) is already reversed by "
            f"{names}. Pass allow_additional_reversal=True if a further "
            f"reversal is genuinely intended."
        )

    if not confirm:
        return {
            "preview": True,
            # See the note in odoo_register_payment: no validator runs for
            # reversals, so no validators_passed key is reported.
            "original": original,
            "journal_id": journal_id,
            "journal_code": journal_code,
            "date": date,
            "reason": reason,
            "existing_reversals": [
                {"move_id": int(e["id"]), "name": e.get("name"), "state": e.get("state")}
                for e in existing
            ],
            "next_step": (
                "Call again with confirm=True to actually reverse this move. "
                "Pass idempotency_key for replay safety. Note that for invoices "
                "and bills Odoo creates the credit note in DRAFT — check the "
                "`reversed` and `reversal_state` fields in the result."
            ),
        }

    audit_params = {
        "move_id": int(move_id),
        "reason": reason,
        "journal_code": journal_code,
        "date": date,
        "confirm": True,
    }

    with audit_call(
        tool="odoo_reverse_move",
        instance=instance,
        params=audit_params,
        idempotency_key=idempotency_key,
        params_fingerprint=fingerprint_params(request_params),
        critical=True,
    ) as ctx:
        # Build wizard vals using only fields the local Odoo accepts —
        # the wizard schema drifted between major versions.
        available = _reversal_field_names(client)
        vals: dict[str, Any] = {"reason": reason}
        if "journal_id" in available:
            vals["journal_id"] = journal_id
        if "date" in available and date:
            vals["date"] = date
        if "move_ids" in available:
            vals["move_ids"] = [(6, 0, [int(move_id)])]

        wizard_id = client.execute_kw(
            "account.move.reversal",
            "create",
            [vals],
            {
                "context": {
                    "active_model": "account.move",
                    "active_ids": [int(move_id)],
                    "active_id": int(move_id),
                }
            },
        )
        client.execute_kw(
            "account.move.reversal",
            "refund_moves",
            [[int(wizard_id)]],
        )
        # The reversal exists in Odoo from here. Mark before reading it back.
        ctx.mark_committed()

        # Read back the wizard to get the IDs of the newly-created reversals.
        # The exact field name has been `new_move_ids` (Odoo 17+) historically.
        wiz_data = client.execute_kw(
            "account.move.reversal", "read", [int(wizard_id)],
            {"fields": ["new_move_ids"]},
        )
        new_ids: list[int] = []
        if wiz_data:
            raw = wiz_data[0].get("new_move_ids") or []
            new_ids = [int(x) for x in raw]

        reversals = [_summarize_move(client, nid) for nid in new_ids]

        # Report what Odoo actually did. refund_moves() only posts+reconciles
        # when move_type == 'entry'; for invoices and bills the credit note is
        # left in draft and the original stays open. Claiming reversed=True
        # there tells the caller a correction landed when nothing did.
        reversal_state = [
            {"move_id": r["move_id"], "name": r.get("name"), "state": r.get("state")}
            for r in reversals
        ]
        fully_posted = bool(reversals) and all(
            r.get("state") == "posted" for r in reversals
        )
        drafts = [r["move_id"] for r in reversals if r.get("state") == "draft"]

        ctx.summary = (
            f"reversed move {move_id} ({original.get('name')}) → "
            f"{[r.get('name') for r in reversals]} "
            f"(posted={fully_posted}, drafts={drafts})"
        )
        result_payload: dict[str, Any] = {
            "reversed": fully_posted,
            "reversal_state": reversal_state,
            "replayed": False,
            "original": original,
            "reversal": reversals,
        }
        if drafts:
            result_payload["next_step"] = (
                f"Reversal(s) {drafts} were created in DRAFT — Odoo only "
                f"auto-posts reversals of manual journal entries. The original "
                f"move is still open. Post them with odoo_post_journal_entry "
                f"to make the correction take effect."
            )
        if not reversals:
            result_payload["next_step"] = (
                "The wizard reported no new moves. Verify in Odoo before "
                "retrying — do not assume nothing happened."
            )
        return result_payload
