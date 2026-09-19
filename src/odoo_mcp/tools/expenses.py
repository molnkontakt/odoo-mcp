"""Expense claims (hr_expense): lookups and a draft-only creation tool.

Small associations and companies get receipts from board members and staff
who paid out of pocket. The treasurer enters those on the person's behalf:
who paid, which category, how much, when, and the receipt itself. Under
Swedish bookkeeping law the receipt is part of the voucher, so
`odoo_create_expense` takes it in the *same* call — an expense without its
receipt is an incomplete entry, and a second call that never comes is how
vouchers end up without documents.

Everything here is draft-only: the expense is created in Odoo's initial
state for a human to submit, approve and post. The generic readers stay closed
for `hr.employee` (it carries private data — home address, identity number,
bank account); `odoo_list_employees` returns only what is needed to pick one.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call
from odoo_mcp.auth import SCOPE_READ, SCOPE_WRITE, requires_scope
from odoo_mcp.client import enter_company_scope, get_client
from odoo_mcp.instances import Instance, resolve_instance
from odoo_mcp.validators import ValidationError

PAYMENT_MODES: frozenset[str] = frozenset({"own_account", "company_account"})


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, list | tuple) and len(value) == 2:
        return int(value[0])
    return None


def _m2o(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_list_employees(
    query: str | None = None,
    company_id: int | None = None,
    limit: int = 50,
    instance: Instance | None = None,
) -> list[dict[str, Any]]:
    """Employees that expenses can be filed for: `employee_id, name, company, work_email`.

    An employee record is per company, so the same person appears once per
    company they belong to — pick the row whose `company` matches the ledger
    the expense belongs to. Only these four fields are returned; the rest of
    `hr.employee` (private address, identity number, bank account) is out of
    scope for this server.

    Args:
        query: case-insensitive substring of the name (optional)
        company_id: restrict to one company (see `odoo_list_companies`)
        limit: max rows (default 50)
        instance: instance name; may be omitted when only one is configured
    """
    client = get_client(instance)
    enter_company_scope(company_id)
    domain: list[Any] = []
    if query:
        domain.append(("name", "ilike", query))
    if company_id:
        domain.append(("company_id", "=", int(company_id)))
    rows = client.execute_kw(
        "hr.employee", "search_read", [domain],
        {"fields": ["id", "name", "company_id", "work_email"], "limit": int(limit), "order": "name, company_id"},
    )
    return [
        {"employee_id": r["id"], "name": r["name"], "company": _m2o(r.get("company_id")),
         "work_email": r.get("work_email") or None}
        for r in rows
    ]


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_list_expense_categories(
    company_id: int | None = None,
    instance: Instance | None = None,
) -> list[dict[str, Any]]:
    """Expense categories (products flagged *can be expensed*): `product_id, code, name, company`.

    `code` is the product's internal reference and is what `odoo_create_expense`
    takes as `category_code`. A category with `company = null` is shared by all
    companies.

    Args:
        company_id: only categories usable in this company (shared ones included)
        instance: instance name; may be omitted when only one is configured
    """
    client = get_client(instance)
    enter_company_scope(company_id)
    domain: list[Any] = [("can_be_expensed", "=", True)]
    if company_id:
        domain.append(("company_id", "in", [False, int(company_id)]))
    rows = client.execute_kw(
        "product.product", "search_read", [domain],
        {"fields": ["id", "name", "default_code", "company_id"], "order": "default_code, name"},
    )
    return [
        {"product_id": r["id"], "code": r.get("default_code") or None, "name": r["name"],
         "company": _m2o(r.get("company_id"))}
        for r in rows
    ]


def _resolve_employee(client: Any, employee_id: int, instance: str) -> dict[str, Any]:
    rows = client.execute_kw(
        "hr.employee", "read", [[int(employee_id)]], {"fields": ["id", "name", "company_id"]},
    )
    if not rows:
        raise ValidationError(f"Employee {employee_id} not found on {instance} (odoo_list_employees)")
    return dict(rows[0])


def _resolve_category(
    client: Any, category_code: str | None, product_id: int | None, company_id: int, instance: str
) -> dict[str, Any]:
    if product_id:
        domain: list[Any] = [("id", "=", int(product_id))]
        label = f"product_id {product_id}"
    elif category_code:
        domain = [("default_code", "=", category_code)]
        label = f"category_code {category_code!r}"
    else:
        raise ValidationError("Give category_code or product_id (odoo_list_expense_categories)")
    rows = client.execute_kw(
        "product.product", "search_read",
        [domain + [("can_be_expensed", "=", True), ("company_id", "in", [False, int(company_id)])]],
        {"fields": ["id", "name", "default_code", "company_id"]},
    )
    if not rows:
        raise ValidationError(
            f"No expense category matches {label} for company {company_id} on {instance}. "
            f"It must be flagged 'can be expensed' and belong to that company or to none "
            f"(odoo_list_expense_categories)."
        )
    if len(rows) > 1:
        ids = ", ".join(str(r["id"]) for r in rows)
        raise ValidationError(f"{label} matches several categories on {instance} (product ids {ids}); pass product_id.")
    return dict(rows[0])


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_expense(
    employee_id: int,
    name: str,
    total_amount: float,
    date: str,
    category_code: str | None = None,
    product_id: int | None = None,
    receipt_base64: str | None = None,
    receipt_filename: str | None = None,
    receipt_mimetype: str | None = None,
    payment_mode: str = "own_account",
    description: str | None = None,
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Create a DRAFT expense claim (hr.expense) for an employee, receipt included.

    Meant for the treasurer or bookkeeper filing a receipt on someone else's
    behalf. The expense lands in Odoo's initial state for a human to submit,
    approve and post; nothing is booked by this call. The company is taken
    from the employee record (an employee belongs to exactly one company, see
    `odoo_list_employees`), and the category must be usable in that company.

    Attach the receipt here rather than in a later call: the document is part
    of the voucher, and the attachment becomes the expense's main attachment
    so it shows in the preview and follows the expense into the journal entry.

    Args:
        employee_id: hr.employee id (`odoo_list_employees`)
        name: what was bought, as it should read in the ledger — shop, receipt
            number and purpose, e.g. "JULA receipt 075797932 — hose and couplings"
        total_amount: amount paid, VAT included, in the company currency
        date: receipt date YYYY-MM-DD
        category_code: expense category by internal reference (`odoo_list_expense_categories`)
        product_id: the category by product id instead of code
        receipt_base64: the receipt (image or PDF), base64-encoded
        receipt_filename: e.g. "kvitto-jula-2026-09-10.jpg" (required with receipt_base64)
        receipt_mimetype: e.g. "image/jpeg" or "application/pdf" (optional)
        payment_mode: "own_account" (default; the employee paid and is to be
            reimbursed) or "company_account" (paid with a company card)
        description: free-text note on the expense (optional)
        instance: instance name; may be omitted when only one is configured

    Returns:
        {expense_id, name, state, employee, company, category, total_amount,
         date, payment_mode, attachment_id}
    """
    if not (name or "").strip():
        raise ValidationError("name must not be empty")
    if float(total_amount) <= 0:
        raise ValidationError("total_amount must be positive")
    if payment_mode not in PAYMENT_MODES:
        raise ValidationError(f"payment_mode must be one of {sorted(PAYMENT_MODES)}")
    if receipt_base64 and not receipt_filename:
        raise ValidationError("receipt_filename is required with receipt_base64")
    if receipt_filename and not receipt_base64:
        raise ValidationError("receipt_base64 is required with receipt_filename")

    instance = resolve_instance(instance)
    client = get_client(instance)
    audit_params = {
        "employee_id": int(employee_id), "name": name, "total_amount": float(total_amount),
        "date": date, "category_code": category_code, "product_id": product_id,
        "payment_mode": payment_mode, "receipt": receipt_filename,
        "receipt_bytes_b64": len(receipt_base64) if receipt_base64 else 0,
    }
    with audit_call(tool="odoo_create_expense", instance=instance, params=audit_params) as ctx:
        employee = _resolve_employee(client, int(employee_id), instance)
        company_id = _m2o_id(employee.get("company_id"))
        if not company_id:
            raise ValidationError(f"Employee {employee_id} has no company on {instance}")
        enter_company_scope(company_id)
        category = _resolve_category(client, category_code, product_id, company_id, instance)

        vals: dict[str, Any] = {
            "name": name.strip(),
            "employee_id": int(employee_id),
            "company_id": company_id,
            "product_id": int(category["id"]),
            "date": date,
            "total_amount_currency": round(float(total_amount), 2),
            "payment_mode": payment_mode,
        }
        if description:
            vals["description"] = description
        expense_id = int(client.execute_kw("hr.expense", "create", [vals]))
        ctx.mark_committed()

        attachment_id: int | None = None
        if receipt_base64:
            att_vals: dict[str, Any] = {
                "name": receipt_filename, "res_model": "hr.expense", "res_id": expense_id,
                "datas": receipt_base64,
            }
            if receipt_mimetype:
                att_vals["mimetype"] = receipt_mimetype
            attachment_id = int(client.execute_kw("ir.attachment", "create", [att_vals]))
            # Odoo only promotes an attachment to the record's main attachment
            # when it arrives through the chatter; set it so the preview and the
            # "missing document" filters see the receipt.
            client.execute_kw(
                "hr.expense", "write", [[expense_id], {"message_main_attachment_id": attachment_id}],
            )

        rows = client.execute_kw(
            "hr.expense", "read", [[expense_id]],
            {"fields": ["name", "state", "total_amount", "date", "currency_id"]},
        )
        row = rows[0] if rows else {}
        ctx.summary = (
            f"created expense id={expense_id} employee={employee['name']} "
            f"total={row.get('total_amount', total_amount)} receipt={'yes' if attachment_id else 'no'}"
        )
        return {
            "expense_id": expense_id,
            "name": row.get("name", vals["name"]),
            "state": row.get("state", "draft"),
            "employee": {"id": int(employee_id), "name": employee["name"]},
            "company": _m2o(employee.get("company_id")),
            "category": {"id": category["id"], "code": category.get("default_code") or None, "name": category["name"]},
            "total_amount": row.get("total_amount", round(float(total_amount), 2)),
            "currency": (_m2o(row.get("currency_id")) or {}).get("name"),
            "date": row.get("date", date),
            "payment_mode": payment_mode,
            "attachment_id": attachment_id,
        }
