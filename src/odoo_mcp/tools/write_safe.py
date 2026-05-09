"""Write tools that create drafts only — they never post or otherwise commit
data that the user can't easily reverse. No `confirm` flag is required, but
every call is audit-logged.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance
from odoo_mcp.validators import (
    JournalEntryPayload,
    JournalLinePayload,
    ValidationError,
    get_registry,
)


def _resolve_journal(client: Any, journal_code: str | None, instance: str) -> int:
    """Resolve a journal by code, defaulting to the company's general/misc journal."""
    domain = [("code", "=", journal_code)] if journal_code else [("type", "=", "general")]
    journals = client.execute_kw(
        "account.journal", "search_read", [domain],
        {"fields": ["id", "code", "name"], "limit": 1},
    )
    if not journals:
        raise ValueError(
            f"No journal found on {instance} (code={journal_code or '<general>'})"
        )
    return int(journals[0]["id"])


def _resolve_account_ids(
    client: Any, codes: list[str], instance: str
) -> dict[str, int]:
    accs = client.execute_kw(
        "account.account", "search_read",
        [[("code", "in", codes)]],
        {"fields": ["id", "code"]},
    )
    by_code = {a["code"]: int(a["id"]) for a in accs}
    missing = [c for c in codes if c not in by_code]
    if missing:
        raise ValueError(
            f"Account code(s) not found on {instance}: {', '.join(missing)}"
        )
    return by_code


def _resolve_tax_tag_ids(
    client: Any, codes: list[str], instance: str
) -> dict[str, int]:
    if not codes:
        return {}
    tags = client.execute_kw(
        "account.account.tag", "search_read",
        [[("name", "in", codes), ("applicability", "=", "taxes")]],
        {"fields": ["id", "name"]},
    )
    by_code = {t["name"]: int(t["id"]) for t in tags}
    missing = [c for c in codes if c not in by_code]
    if missing:
        raise ValueError(
            f"Tax tag(s) not found on {instance}: {', '.join(missing)}"
        )
    return by_code


@mcp.tool()
def odoo_create_journal_entry_draft(
    instance: Instance,
    date: str,
    lines: list[dict[str, Any]],
    ref: str | None = None,
    journal_code: str | None = None,
) -> dict[str, Any]:
    """Create an account.move in `draft` state with the given lines.

    Lines must balance: sum(debit) == sum(credit). The entry is created as
    a draft so a human can review it before posting (use `odoo_post_journal_entry`
    in Phase 3 once that tool exists; for now post via the Odoo UI).

    Args:
        instance: "prod" or "dev"
        date: YYYY-MM-DD
        lines: list of line dicts. Each line:
            account_code: str (required)  — BAS/CoA code
            debit: float (default 0)
            credit: float (default 0)
            name: str | None              — line description
            tax_tag_codes: list[str]      — e.g. ["se_30", "se_48"]
            partner_id: int | None
        ref: optional reference / description on the move
        journal_code: optional journal code; defaults to the first `general` journal

    Returns:
        {move_id, name, state, line_count}

    Raises:
        ValidationError if the payload doesn't balance or references unknown
        accounts/tags.
    """
    if not lines:
        raise ValidationError("lines must not be empty")

    line_payloads = [
        JournalLinePayload(
            account_code=str(line["account_code"]),
            debit=float(line.get("debit", 0) or 0),
            credit=float(line.get("credit", 0) or 0),
            name=line.get("name"),
            tax_tag_codes=list(line.get("tax_tag_codes") or []),
            partner_id=line.get("partner_id"),
        )
        for line in lines
    ]
    payload = JournalEntryPayload(
        instance=instance,
        date=date,
        ref=ref,
        journal_code=journal_code,
        lines=line_payloads,
    )

    client = get_client(instance)

    audit_params = {
        "date": date,
        "ref": ref,
        "journal_code": journal_code,
        "line_count": len(line_payloads),
        "debit_total": sum(line.debit for line in line_payloads),
        "credit_total": sum(line.credit for line in line_payloads),
    }
    with audit_call(
        tool="odoo_create_journal_entry_draft",
        instance=instance,
        params=audit_params,
    ) as ctx:
        get_registry().run(payload, client)

        journal_id = _resolve_journal(client, journal_code, instance)
        account_ids = _resolve_account_ids(
            client,
            sorted({line.account_code for line in line_payloads}),
            instance,
        )
        all_tag_codes = sorted({tag for line in line_payloads for tag in line.tax_tag_codes})
        tag_ids = _resolve_tax_tag_ids(client, all_tag_codes, instance)

        line_vals: list[tuple[int, int, dict[str, Any]]] = []
        for line in line_payloads:
            vals: dict[str, Any] = {
                "account_id": account_ids[line.account_code],
                "name": line.name or (ref or "Journal entry"),
                "debit": round(line.debit, 2),
                "credit": round(line.credit, 2),
            }
            if line.partner_id:
                vals["partner_id"] = int(line.partner_id)
            if line.tax_tag_codes:
                vals["tax_tag_ids"] = [
                    (6, 0, [tag_ids[c] for c in line.tax_tag_codes])
                ]
            line_vals.append((0, 0, vals))

        move_vals: dict[str, Any] = {
            "journal_id": journal_id,
            "date": date,
            "move_type": "entry",
            "line_ids": line_vals,
        }
        if ref:
            move_vals["ref"] = ref

        move_id = client.execute_kw("account.move", "create", [move_vals])
        result = client.execute_kw(
            "account.move", "read", [int(move_id)],
            {"fields": ["id", "name", "state"]},
        )
        ctx.summary = f"created move id={move_id}"
        return {
            "move_id": int(move_id),
            "name": result[0]["name"] if result else None,
            "state": result[0]["state"] if result else "draft",
            "line_count": len(line_payloads),
        }


@mcp.tool()
def odoo_add_tax_tags(
    instance: Instance,
    line_id: int,
    tag_codes: list[str],
    replace: bool = False,
) -> dict[str, Any]:
    """Add (or replace) tax tags on a single account.move.line.

    Only works on lines whose parent move is in `draft` state — Odoo locks
    posted moves' tax tags as part of audit-trail rules.

    Args:
        instance: "prod" or "dev"
        line_id: account.move.line ID
        tag_codes: tag short codes, e.g. ["se_30", "se_48"]
        replace: if True, overwrite existing tags. If False (default), add to existing.

    Returns:
        {line_id, applied_tags}
    """
    client = get_client(instance)

    with audit_call(
        tool="odoo_add_tax_tags",
        instance=instance,
        params={"line_id": line_id, "tag_codes": tag_codes, "replace": replace},
    ) as ctx:
        # Verify parent move is draft
        line_rows = client.execute_kw(
            "account.move.line", "read", [int(line_id)],
            {"fields": ["id", "move_id", "parent_state", "tax_tag_ids"]},
        )
        if not line_rows:
            raise ValueError(f"Line {line_id} not found on {instance}")
        line = line_rows[0]
        if line.get("parent_state") != "draft":
            raise ValidationError(
                f"Line {line_id} belongs to a posted move; tax tags are locked. "
                f"Reverse and re-create the move instead."
            )

        tag_ids = _resolve_tax_tag_ids(client, list(tag_codes), instance)
        if replace:
            command = [(6, 0, sorted(tag_ids.values()))]
        else:
            command = [(4, tid) for tid in tag_ids.values()]

        client.execute_kw(
            "account.move.line", "write",
            [[int(line_id)], {"tax_tag_ids": command}],
        )
        ctx.summary = f"line {line_id} tags={','.join(tag_codes)} replace={replace}"
        return {"line_id": int(line_id), "applied_tags": list(tag_codes)}


@mcp.tool()
def odoo_set_partner(
    instance: Instance,
    move_id: int,
    partner_id: int,
) -> dict[str, Any]:
    """Set the partner on a draft account.move.

    Args:
        instance: "prod" or "dev"
        move_id: account.move ID
        partner_id: res.partner ID
    """
    client = get_client(instance)

    with audit_call(
        tool="odoo_set_partner",
        instance=instance,
        params={"move_id": move_id, "partner_id": partner_id},
    ) as ctx:
        moves = client.execute_kw(
            "account.move", "read", [int(move_id)],
            {"fields": ["state"]},
        )
        if not moves:
            raise ValueError(f"Move {move_id} not found on {instance}")
        if moves[0]["state"] != "draft":
            raise ValidationError(
                f"Move {move_id} is not a draft (state={moves[0]['state']}); "
                f"partner can only be changed on drafts."
            )
        # Verify partner exists
        partners = client.execute_kw(
            "res.partner", "read", [int(partner_id)], {"fields": ["id", "name"]},
        )
        if not partners:
            raise ValueError(f"Partner {partner_id} not found on {instance}")

        client.execute_kw(
            "account.move", "write",
            [[int(move_id)], {"partner_id": int(partner_id)}],
        )
        ctx.summary = f"move {move_id} partner_id={partner_id} ({partners[0]['name']})"
        return {
            "move_id": int(move_id),
            "partner_id": int(partner_id),
            "partner_name": partners[0]["name"],
        }
