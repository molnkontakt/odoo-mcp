"""Task-shaped read tools for the person who chases the money.

The generic readers answer "give me rows"; these answer the questions an
accountant actually asks: who has not paid, what is overdue and by how much,
which bank lines are still unreconciled. Everything here is read-only and
scoped like `read.py`; the caller's Odoo rights still decide what is visible.

Field names that only exist with optional modules (`reminder_*` from
account_invoice_reminder) are probed once per call and left out when absent,
so the tools work on a plain Odoo too.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.auth import SCOPE_READ, requires_scope
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance

_OPEN_STATES = ("not_paid", "partial")


def _has_fields(client: Any, model: str, names: list[str]) -> set[str]:
    present = client.execute_kw(model, "fields_get", [names], {"attributes": ["type"]})
    return set(present)


def _m2o(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    return None


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_overdue_invoices(
    instance: Instance,
    company_id: int | None = None,
    as_of: str | None = None,
    min_days_overdue: int = 0,
    partner_id: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Posted customer invoices past their due date that still have a balance.

    Returns one row per invoice with days overdue and, when the payment
    reminder module is installed, the last reminder level/date and the next
    level that would apply today. Sorted oldest due date first.

    Args:
        instance: instance name
        company_id: restrict to one company (see `odoo_list_companies`)
        as_of: reference date YYYY-MM-DD (default: today)
        min_days_overdue: skip invoices overdue fewer days than this
        partner_id: one customer only
        limit: max rows (default 200)
    """
    client = get_client(instance)
    today = date.fromisoformat(as_of) if as_of else date.today()
    domain: list[Any] = [
        ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
        ("payment_state", "in", list(_OPEN_STATES)), ("invoice_date_due", "<", today.isoformat()),
    ]
    if company_id:
        domain.append(("company_id", "=", company_id))
    if partner_id:
        domain.append(("partner_id", "=", partner_id))
    extra = _has_fields(client, "account.move", ["reminder_level_id", "reminder_date", "reminder_count", "reminder_next_level_id"])
    fields = ["id", "name", "ref", "partner_id", "company_id", "invoice_date", "invoice_date_due",
              "amount_total", "amount_residual", "currency_id"] + sorted(extra)
    rows = client.execute_kw("account.move", "search_read", [domain], {"fields": fields, "order": "invoice_date_due, id", "limit": limit})
    out = []
    for r in rows:
        due = date.fromisoformat(r["invoice_date_due"])
        days = (today - due).days
        if days < min_days_overdue:
            continue
        item = {
            "move_id": r["id"], "name": r["name"], "ref": r.get("ref"),
            "partner": _m2o(r["partner_id"]), "company": _m2o(r["company_id"]),
            "invoice_date": r["invoice_date"], "due_date": r["invoice_date_due"], "days_overdue": days,
            "amount_total": r["amount_total"], "amount_residual": r["amount_residual"],
            "currency": (_m2o(r["currency_id"]) or {}).get("name"),
        }
        if extra:
            item["last_reminder"] = {"level": (_m2o(r.get("reminder_level_id")) or {}).get("name"), "date": r.get("reminder_date") or None,
                                     "count": r.get("reminder_count") or 0}
            item["next_reminder_level"] = (_m2o(r.get("reminder_next_level_id")) or {}).get("name")
        out.append(item)
    return {
        "as_of": today.isoformat(), "count": len(out),
        "total_residual": round(sum(i["amount_residual"] for i in out), 2),
        "invoices": out,
    }


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_unpaid_by_customer(
    instance: Instance,
    company_id: int | None = None,
    include_not_due: bool = True,
    limit: int = 500,
) -> dict[str, Any]:
    """Open customer balances grouped per customer: who owes what.

    One row per customer with the open invoices, total residual, the oldest
    due date and whether anything is overdue. Answers "who has not paid the
    annual fee" without the caller building domains.

    Args:
        instance: instance name
        company_id: restrict to one company
        include_not_due: also list invoices that are open but not yet due (default True)
        limit: max invoices scanned (default 500)
    """
    client = get_client(instance)
    today = date.today().isoformat()
    domain: list[Any] = [("move_type", "=", "out_invoice"), ("state", "=", "posted"), ("payment_state", "in", list(_OPEN_STATES))]
    if company_id:
        domain.append(("company_id", "=", company_id))
    if not include_not_due:
        domain.append(("invoice_date_due", "<", today))
    rows = client.execute_kw("account.move", "search_read", [domain], {
        "fields": ["id", "name", "partner_id", "invoice_date_due", "amount_residual", "company_id"],
        "order": "partner_id, invoice_date_due", "limit": limit})
    groups: dict[int, dict[str, Any]] = {}
    for r in rows:
        p = _m2o(r["partner_id"]) or {"id": 0, "name": "?"}
        g = groups.setdefault(p["id"], {"partner": p, "company": _m2o(r["company_id"]), "invoices": [], "total_residual": 0.0,
                                        "oldest_due": None, "overdue": False})
        g["invoices"].append({"move_id": r["id"], "name": r["name"], "due_date": r["invoice_date_due"], "amount_residual": r["amount_residual"]})
        g["total_residual"] = round(g["total_residual"] + r["amount_residual"], 2)
        if r["invoice_date_due"] and (g["oldest_due"] is None or r["invoice_date_due"] < g["oldest_due"]):
            g["oldest_due"] = r["invoice_date_due"]
        if r["invoice_date_due"] and r["invoice_date_due"] < today:
            g["overdue"] = True
    customers = sorted(groups.values(), key=lambda g: (not g["overdue"], g["oldest_due"] or "9999", g["partner"]["name"]))
    return {"as_of": today, "customers": len(customers), "invoices": len(rows),
            "total_residual": round(sum(g["total_residual"] for g in customers), 2), "by_customer": customers}


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_unreconciled_bank_lines(
    instance: Instance,
    company_id: int | None = None,
    journal_code: str | None = None,
    date_from: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Bank statement lines that are not reconciled yet, oldest first.

    For each line: date, label, amount, partner (if the import set one) and
    the statement it belongs to. This is the "what is left to match" view.

    Args:
        instance: instance name
        company_id: restrict to one company
        journal_code: one bank journal (e.g. "BNK1"); with several companies pass company_id too
        date_from: YYYY-MM-DD, skip older lines
        limit: max rows (default 200)
    """
    client = get_client(instance)
    domain: list[Any] = [("is_reconciled", "=", False)]
    if company_id:
        domain.append(("company_id", "=", company_id))
    if journal_code:
        domain.append(("journal_id.code", "=", journal_code))
    if date_from:
        domain.append(("date", ">=", date_from))
    rows = client.execute_kw("account.bank.statement.line", "search_read", [domain], {
        "fields": ["id", "date", "payment_ref", "amount", "partner_id", "journal_id", "statement_id", "company_id"],
        "order": "date, id", "limit": limit})
    by_journal: dict[str, float] = defaultdict(float)
    lines = []
    for r in rows:
        j = _m2o(r["journal_id"]) or {"name": "?"}
        by_journal[j["name"]] = round(by_journal[j["name"]] + r["amount"], 2)
        lines.append({"line_id": r["id"], "date": r["date"], "label": r["payment_ref"], "amount": r["amount"],
                      "partner": _m2o(r["partner_id"]), "journal": j, "statement": _m2o(r["statement_id"]), "company": _m2o(r["company_id"])})
    return {"count": len(lines), "net_by_journal": dict(by_journal), "lines": lines}


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_customer_statement(
    instance: Instance,
    partner_id: int,
    company_id: int | None = None,
    include_paid: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """One customer's invoices, credit notes and what remains to pay.

    Args:
        instance: instance name
        partner_id: the customer (commercial partner or contact)
        company_id: restrict to one company
        include_paid: also list settled documents (default False)
        limit: max rows (default 100)
    """
    client = get_client(instance)
    domain: list[Any] = [("move_type", "in", ["out_invoice", "out_refund"]), ("state", "=", "posted"),
                         "|", ("partner_id", "=", partner_id), ("partner_id.commercial_partner_id", "=", partner_id)]
    if company_id:
        domain.append(("company_id", "=", company_id))
    if not include_paid:
        domain.append(("payment_state", "in", list(_OPEN_STATES)))
    extra = _has_fields(client, "account.move", ["reminder_level_id", "reminder_date"])
    fields = ["id", "name", "ref", "move_type", "invoice_date", "invoice_date_due", "amount_total", "amount_residual",
              "payment_state", "company_id"] + sorted(extra)
    rows = client.execute_kw("account.move", "search_read", [domain], {"fields": fields, "order": "invoice_date, id", "limit": limit})
    partner = client.execute_kw("res.partner", "read", [partner_id], {"fields": ["name", "email", "phone"]})
    docs = []
    for r in rows:
        sign = -1 if r["move_type"] == "out_refund" else 1
        d = {"move_id": r["id"], "name": r["name"], "type": r["move_type"], "ref": r.get("ref"), "date": r["invoice_date"],
             "due_date": r["invoice_date_due"], "amount_total": sign * r["amount_total"], "amount_residual": sign * r["amount_residual"],
             "payment_state": r["payment_state"], "company": _m2o(r["company_id"])}
        if extra:
            d["last_reminder"] = {"level": (_m2o(r.get("reminder_level_id")) or {}).get("name"), "date": r.get("reminder_date") or None}
        docs.append(d)
    return {"partner": partner[0] if partner else {"id": partner_id}, "documents": docs,
            "balance_due": round(sum(d["amount_residual"] for d in docs), 2)}
