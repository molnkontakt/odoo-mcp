"""Write tools that create drafts only — they never post or otherwise commit
data that the user can't easily reverse. No `confirm` flag is required.

Calls are audit-logged when `MCP_AUDIT_DB_URL` is configured (see
`audit.py`); otherwise audit logging is a silent no-op.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call
from odoo_mcp.auth import SCOPE_WRITE, requires_scope
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance
from odoo_mcp.validators import (
    InvoiceLinePayload,
    InvoicePayload,
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
        raise ValidationError(
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
        raise ValidationError(
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
        raise ValidationError(
            f"Tax tag(s) not found on {instance}: {', '.join(missing)}"
        )
    return by_code


def _resolve_invoice_tax_ids(
    client: Any, names: list[str], move_type: str, instance: str
) -> dict[str, int]:
    """Resolve account.tax by name, constrained to the invoice's direction.

    out_invoice/out_refund -> sale taxes; in_invoice/in_refund -> purchase taxes.
    Disambiguates same-named sale/purchase taxes AND refuses a wrong-direction
    tax outright, so an agent can't silently book incorrect VAT into the
    momsrapport.
    """
    if not names:
        return {}
    expected = "sale" if move_type in ("out_invoice", "out_refund") else "purchase"
    taxes = client.execute_kw(
        "account.tax", "search_read",
        [[("name", "in", names), ("type_tax_use", "in", [expected, "none"])]],
        {"fields": ["id", "name", "type_tax_use"]},
    )
    # Prefer an exact-direction match over a 'none' tax when both share a name.
    by_name: dict[str, int] = {}
    for t in sorted(taxes, key=lambda x: 0 if x["type_tax_use"] == expected else 1):
        by_name.setdefault(t["name"], int(t["id"]))
    missing = [n for n in names if n not in by_name]
    if missing:
        raise ValidationError(
            f"Tax(es) not found as '{expected}' taxes on {instance}: {', '.join(missing)}. "
            f"The invoice is '{move_type}'; pick a {expected} tax "
            f"(odoo_list_taxes type_tax_use='{expected}')."
        )
    return by_name


@mcp.tool()
@requires_scope(SCOPE_WRITE)
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
@requires_scope(SCOPE_WRITE)
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
@requires_scope(SCOPE_WRITE)
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


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_invoice(
    instance: Instance,
    move_type: str,
    partner_id: int,
    lines: list[dict[str, Any]],
    invoice_date: str | None = None,
    ref: str | None = None,
    journal_code: str | None = None,
) -> dict[str, Any]:
    """Create a DRAFT customer/vendor invoice (or refund).

    Odoo computes the tax lines and totals from each line's taxes, so this is
    the correct way to make a VAT-bearing invoice (unlike a raw journal entry).
    Created as `draft` for human review — post it via the Odoo UI or a future
    critical-write tool.

    Args:
        instance: "prod" or "dev"
        move_type: "out_invoice" (customer), "in_invoice" (vendor bill),
            "out_refund" (credit note), or "in_refund" (vendor credit).
        partner_id: res.partner ID (customer or vendor).
        lines: list of line dicts. Each line:
            name: str (required)      — description
            price_unit: float (required)
            quantity: float (default 1)
            account_code: str | None  — income/expense account; omit to let Odoo
                                        derive it from the product/journal.
            tax_names: list[str]      — account.tax names; MUST match the invoice
                                        direction (sale for out_*, purchase for
                                        in_*). Omit to let Odoo derive from product.
            product_id: int | None
        invoice_date: YYYY-MM-DD (defaults to Odoo's today on post).
        ref: reference (e.g. the vendor's invoice number).
        journal_code: optional; omit to let Odoo pick the default sale/purchase journal.

    Returns:
        {move_id, name, state, move_type, amount_untaxed, amount_tax,
         amount_total, line_count}

    Raises:
        ValidationError on bad move_type, empty/invalid lines, unknown accounts,
        or a tax whose direction doesn't match the invoice.
    """
    line_payloads = [
        InvoiceLinePayload(
            name=str(line["name"]),
            price_unit=float(line["price_unit"]),
            quantity=float(line.get("quantity", 1) or 1),
            account_code=str(line["account_code"]) if line.get("account_code") else None,
            tax_names=list(line.get("tax_names") or []),
            product_id=line.get("product_id"),
        )
        for line in lines
    ]
    payload = InvoicePayload(
        instance=instance,
        move_type=move_type,
        partner_id=int(partner_id),
        lines=line_payloads,
        invoice_date=invoice_date,
        ref=ref,
        journal_code=journal_code,
    )

    client = get_client(instance)
    audit_params = {
        "move_type": move_type,
        "partner_id": int(partner_id),
        "invoice_date": invoice_date,
        "ref": ref,
        "line_count": len(line_payloads),
    }
    with audit_call(
        tool="odoo_create_invoice", instance=instance, params=audit_params
    ) as ctx:
        get_registry().run_invoice(payload, client)

        partners = client.execute_kw(
            "res.partner", "read", [int(partner_id)], {"fields": ["id", "name"]}
        )
        if not partners:
            raise ValidationError(f"Partner {partner_id} not found on {instance}")

        account_codes = sorted(
            {line.account_code for line in line_payloads if line.account_code}
        )
        account_ids = (
            _resolve_account_ids(client, account_codes, instance)
            if account_codes
            else {}
        )
        tax_names = sorted({t for line in line_payloads for t in line.tax_names})
        tax_ids = _resolve_invoice_tax_ids(client, tax_names, move_type, instance)

        line_vals: list[tuple[int, int, dict[str, Any]]] = []
        for line in line_payloads:
            vals: dict[str, Any] = {
                "name": line.name,
                "quantity": line.quantity,
                "price_unit": line.price_unit,
            }
            if line.product_id:
                vals["product_id"] = int(line.product_id)
            if line.account_code:
                vals["account_id"] = account_ids[line.account_code]
            if line.tax_names:
                vals["tax_ids"] = [(6, 0, [tax_ids[t] for t in line.tax_names])]
            line_vals.append((0, 0, vals))

        move_vals: dict[str, Any] = {
            "move_type": move_type,
            "partner_id": int(partner_id),
            "invoice_line_ids": line_vals,
        }
        if invoice_date:
            move_vals["invoice_date"] = invoice_date
        if ref:
            move_vals["ref"] = ref
        if journal_code:
            move_vals["journal_id"] = _resolve_journal(client, journal_code, instance)

        move_id = client.execute_kw("account.move", "create", [move_vals])
        res = client.execute_kw(
            "account.move", "read", [int(move_id)],
            {"fields": ["id", "name", "state", "move_type",
                        "amount_untaxed", "amount_tax", "amount_total"]},
        )
        row = res[0] if res else {}
        ctx.summary = f"created {move_type} id={move_id} total={row.get('amount_total')}"
        return {
            "move_id": int(move_id),
            "name": row.get("name"),
            "state": row.get("state", "draft"),
            "move_type": move_type,
            "amount_untaxed": row.get("amount_untaxed"),
            "amount_tax": row.get("amount_tax"),
            "amount_total": row.get("amount_total"),
            "line_count": len(line_payloads),
        }


_INVOICE_UPDATABLE_FIELDS = {
    "partner_id", "invoice_date", "invoice_date_due",
    "ref", "narration", "payment_reference",
}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_update_invoice(
    instance: Instance,
    move_id: int,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Update header fields on a DRAFT invoice/move. Rejected on posted moves.

    Only a safe whitelist of header fields can be changed (partner_id,
    invoice_date, invoice_date_due, ref, narration, payment_reference). Editing
    lines/taxes is deliberately out of scope — recreate the invoice instead.

    Returns:
        {move_id, updated}
    """
    if not values:
        raise ValidationError("values must not be empty")
    bad = set(values) - _INVOICE_UPDATABLE_FIELDS
    if bad:
        raise ValidationError(
            f"Cannot update field(s) via this tool: {', '.join(sorted(bad))}. "
            f"Allowed: {', '.join(sorted(_INVOICE_UPDATABLE_FIELDS))}."
        )

    client = get_client(instance)
    with audit_call(
        tool="odoo_update_invoice", instance=instance,
        params={"move_id": move_id, "fields": sorted(values)},
    ) as ctx:
        moves = client.execute_kw(
            "account.move", "read", [int(move_id)], {"fields": ["state"]}
        )
        if not moves:
            raise ValueError(f"Move {move_id} not found on {instance}")
        if moves[0]["state"] != "draft":
            raise ValidationError(
                f"Move {move_id} is not a draft (state={moves[0]['state']}); "
                f"only drafts can be edited."
            )
        if "partner_id" in values:
            partners = client.execute_kw(
                "res.partner", "read", [int(values["partner_id"])], {"fields": ["id"]}
            )
            if not partners:
                raise ValidationError(
                    f"Partner {values['partner_id']} not found on {instance}"
                )
        client.execute_kw("account.move", "write", [[int(move_id)], values])
        ctx.summary = f"move {move_id} updated {','.join(sorted(values))}"
        return {"move_id": int(move_id), "updated": sorted(values)}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_partner(
    instance: Instance,
    name: str,
    is_company: bool = True,
    vat: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    street: str | None = None,
    city: str | None = None,
    zip_code: str | None = None,
    country_code: str | None = None,
    customer: bool = False,
    supplier: bool = False,
) -> dict[str, Any]:
    """Create a res.partner (customer/vendor/contact).

    Args:
        instance: "prod" or "dev"
        name: partner name (required)
        is_company: True for an organisation (default), False for an individual
        vat: VAT / org number, e.g. "SE556677889901"
        email, phone, street, city: contact fields
        zip_code: postal code (maps to Odoo `zip`)
        country_code: ISO code, e.g. "SE" (resolved to country_id)
        customer: if True, set customer_rank=1
        supplier: if True, set supplier_rank=1

    Returns:
        {partner_id, name}
    """
    client = get_client(instance)
    with audit_call(
        tool="odoo_create_partner", instance=instance,
        params={"name": name, "vat": vat, "is_company": is_company},
    ) as ctx:
        vals: dict[str, Any] = {"name": name, "is_company": is_company}
        for key, val in (
            ("vat", vat), ("email", email), ("phone", phone),
            ("street", street), ("city", city), ("zip", zip_code),
        ):
            if val:
                vals[key] = val
        if country_code:
            countries = client.execute_kw(
                "res.country", "search_read",
                [[("code", "=", country_code.upper())]],
                {"fields": ["id"], "limit": 1},
            )
            if not countries:
                raise ValidationError(f"Country code '{country_code}' not found")
            vals["country_id"] = countries[0]["id"]
        if customer:
            vals["customer_rank"] = 1
        if supplier:
            vals["supplier_rank"] = 1

        partner_id = client.execute_kw("res.partner", "create", [vals])
        ctx.summary = f"created partner id={partner_id} ({name})"
        return {"partner_id": int(partner_id), "name": name}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_product(
    instance: Instance,
    name: str,
    list_price: float = 0.0,
    default_code: str | None = None,
    product_type: str = "service",
    sale_ok: bool = True,
    purchase_ok: bool = False,
) -> dict[str, Any]:
    """Create a product.product.

    Args:
        instance: "prod" or "dev"
        name: product name (required)
        list_price: sales price (default 0)
        default_code: internal reference / SKU
        product_type: "service" (default) or "consu"
        sale_ok: sellable (default True)
        purchase_ok: purchasable (default False)

    Returns:
        {product_id, name}
    """
    client = get_client(instance)
    with audit_call(
        tool="odoo_create_product", instance=instance,
        params={"name": name, "default_code": default_code, "type": product_type},
    ) as ctx:
        vals: dict[str, Any] = {
            "name": name,
            "list_price": list_price,
            "type": product_type,
            "sale_ok": sale_ok,
            "purchase_ok": purchase_ok,
        }
        if default_code:
            vals["default_code"] = default_code
        product_id = client.execute_kw("product.product", "create", [vals])
        ctx.summary = f"created product id={product_id} ({name})"
        return {"product_id": int(product_id), "name": name}


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_upload_attachment(
    instance: Instance,
    res_model: str,
    res_id: int,
    filename: str,
    data_base64: str,
    mimetype: str | None = None,
) -> dict[str, Any]:
    """Attach a base64-encoded file to any record (e.g. a PDF onto an invoice).

    Feeds the OCR/invoice flow: upload a supplier PDF onto the draft bill.

    Args:
        instance: "prod" or "dev"
        res_model: model to attach to, e.g. "account.move"
        res_id: record id
        filename: display name, e.g. "invoice_123.pdf"
        data_base64: file contents, base64-encoded (string)
        mimetype: optional, e.g. "application/pdf"

    Returns:
        {attachment_id, name, res_model, res_id}
    """
    client = get_client(instance)
    with audit_call(
        tool="odoo_upload_attachment", instance=instance,
        params={"res_model": res_model, "res_id": int(res_id),
                "filename": filename, "bytes_b64": len(data_base64)},
    ) as ctx:
        vals: dict[str, Any] = {
            "name": filename,
            "res_model": res_model,
            "res_id": int(res_id),
            "datas": data_base64,
        }
        if mimetype:
            vals["mimetype"] = mimetype
        attachment_id = client.execute_kw("ir.attachment", "create", [vals])
        ctx.summary = f"attachment {attachment_id} '{filename}' -> {res_model},{res_id}"
        return {
            "attachment_id": int(attachment_id),
            "name": filename,
            "res_model": res_model,
            "res_id": int(res_id),
        }
