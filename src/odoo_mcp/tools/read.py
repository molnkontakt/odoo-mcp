"""Read-only tools — no confirmation needed, always safe."""

from typing import Any

from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance
from odoo_mcp.server import mcp


@mcp.tool()
def odoo_search_partners(
    instance: Instance,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search res.partner by name or VAT (case-insensitive ilike).

    Args:
        instance: "prod" or "dev"
        query: substring match against name OR vat
        limit: max results (default 20)

    Returns:
        List of {id, name, vat, country_code, is_company}
    """
    client = get_client(instance)
    domain = ["|", ("name", "ilike", query), ("vat", "ilike", query)]
    fields = ["id", "name", "vat", "country_id", "is_company"]
    rows = client.execute_kw(
        "res.partner", "search_read", [domain], {"fields": fields, "limit": limit}
    )
    # Flatten country_id [id, name] tuple to country_code lookup
    for r in rows:
        if r.get("country_id"):
            country = client.execute_kw(
                "res.country", "read", [r["country_id"][0]], {"fields": ["code"]}
            )
            r["country_code"] = country[0]["code"] if country else None
            del r["country_id"]
    return rows


@mcp.tool()
def odoo_search_invoices(
    instance: Instance,
    date_from: str,
    date_to: str,
    move_type: str | None = None,
    state: str | None = None,
    partner_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search account.move (invoices and journal entries).

    Args:
        instance: "prod" or "dev"
        date_from: YYYY-MM-DD inclusive
        date_to: YYYY-MM-DD inclusive
        move_type: optional filter, e.g. "in_invoice", "out_invoice", "entry"
        state: optional, "draft" or "posted"
        partner_id: optional partner filter
        limit: max results (default 50)
    """
    client = get_client(instance)
    domain = [("date", ">=", date_from), ("date", "<=", date_to)]
    if move_type:
        domain.append(("move_type", "=", move_type))
    if state:
        domain.append(("state", "=", state))
    if partner_id:
        domain.append(("partner_id", "=", partner_id))
    fields = ["id", "name", "ref", "date", "state", "move_type",
              "partner_id", "amount_total", "amount_residual", "currency_id"]
    return client.execute_kw(
        "account.move", "search_read", [domain],
        {"fields": fields, "limit": limit, "order": "date desc"},
    )


@mcp.tool()
def odoo_get_invoice(instance: Instance, move_id: int) -> dict[str, Any]:
    """Get full account.move with all journal lines.

    Returns header fields plus a `lines` array with each move_line:
      account_code, name, debit, credit, partner_id, tax_tag_codes
    """
    client = get_client(instance)
    moves = client.execute_kw(
        "account.move", "read", [move_id],
        {"fields": ["id", "name", "ref", "date", "state", "move_type",
                    "partner_id", "amount_total", "amount_residual",
                    "currency_id", "journal_id", "line_ids"]},
    )
    if not moves:
        raise ValueError(f"Move {move_id} not found on {instance}")
    move = moves[0]

    line_data = client.execute_kw(
        "account.move.line", "read", [move["line_ids"]],
        {"fields": ["id", "name", "account_id", "debit", "credit",
                    "partner_id", "tax_tag_ids"]},
    )
    # Resolve account → code, tags → codes
    acc_ids = list({line["account_id"][0] for line in line_data if line.get("account_id")})
    acc_map = {}
    if acc_ids:
        accs = client.execute_kw(
            "account.account", "read", [acc_ids], {"fields": ["id", "code"]}
        )
        acc_map = {a["id"]: a["code"] for a in accs}

    tag_ids = list({tid for line in line_data for tid in (line.get("tax_tag_ids") or [])})
    tag_map = {}
    if tag_ids:
        tags = client.execute_kw(
            "account.account.tag", "read", [tag_ids], {"fields": ["id", "name"]}
        )
        tag_map = {t["id"]: t["name"] for t in tags}

    move["lines"] = [
        {
            "id": line["id"],
            "name": line["name"],
            "account_code": acc_map.get(line["account_id"][0]) if line.get("account_id") else None,
            "debit": line["debit"],
            "credit": line["credit"],
            "partner_id": line["partner_id"][0] if line.get("partner_id") else None,
            "tax_tag_codes": [tag_map[tid] for tid in (line.get("tax_tag_ids") or []) if tid in tag_map],
        }
        for line in line_data
    ]
    del move["line_ids"]
    return move


@mcp.tool()
def odoo_get_account_balance(
    instance: Instance,
    account_code: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Sum debit-credit on account.move.line for the given account code.

    Uses debit-credit (not balance) — `balance` is a cached field that can
    drift from `debit - credit` for foreign-currency invoices in some Odoo
    versions, while debit/credit are always authoritative in company currency.

    Args:
        instance: "prod" or "dev"
        account_code: BAS account code, e.g. "2611" or "6231"
        date_from: optional start date inclusive
        date_to: optional end date inclusive

    Returns:
        {account_code, account_name, debit_sum, credit_sum, balance}
    """
    client = get_client(instance)
    accs = client.execute_kw(
        "account.account", "search_read",
        [[("code", "=", account_code)]],
        {"fields": ["id", "code", "name"], "limit": 1},
    )
    if not accs:
        raise ValueError(f"Account {account_code} not found on {instance}")
    acc = accs[0]

    domain: list[Any] = [("account_id", "=", acc["id"]),
                         ("parent_state", "=", "posted")]
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))

    lines = client.execute_kw(
        "account.move.line", "search_read", [domain],
        {"fields": ["debit", "credit"]},
    )
    debit = sum(line["debit"] for line in lines)
    credit = sum(line["credit"] for line in lines)
    return {
        "account_code": acc["code"],
        "account_name": acc["name"],
        "debit_sum": round(debit, 2),
        "credit_sum": round(credit, 2),
        "balance": round(debit - credit, 2),
        "line_count": len(lines),
    }
