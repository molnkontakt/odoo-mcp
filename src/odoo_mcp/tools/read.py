"""Read-only tools — no confirmation needed, always safe."""

from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance


def _resolve_country_codes(client: Any, partner_rows: list[dict[str, Any]]) -> None:
    """Replace `country_id` (Odoo [id, name] tuple) with `country_code` in-place.

    Mutates each partner row: removes `country_id`, sets `country_code` to the
    ISO code or None. Single batched lookup avoids N+1 queries.
    """
    country_ids = list({
        row["country_id"][0]
        for row in partner_rows
        if row.get("country_id") and isinstance(row["country_id"], (list, tuple))
    })
    code_map: dict[int, str] = {}
    if country_ids:
        countries = client.execute_kw(
            "res.country", "read", [country_ids], {"fields": ["id", "code"]}
        )
        code_map = {c["id"]: c["code"] for c in countries}

    for row in partner_rows:
        cid = row.get("country_id")
        if cid and isinstance(cid, (list, tuple)):
            row["country_code"] = code_map.get(cid[0])
        else:
            row["country_code"] = None
        row.pop("country_id", None)


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
        List of {id, name, vat, country_code, is_company}.
        `country_code` is always present (None when partner has no country).
    """
    client = get_client(instance)
    domain = ["|", ("name", "ilike", query), ("vat", "ilike", query)]
    fields = ["id", "name", "vat", "country_id", "is_company"]
    rows = client.execute_kw(
        "res.partner", "search_read", [domain], {"fields": fields, "limit": limit}
    )
    _resolve_country_codes(client, rows)
    return rows


@mcp.tool()
def odoo_get_partner(instance: Instance, partner_id: int) -> dict[str, Any]:
    """Get full info for a single res.partner.

    Args:
        instance: "prod" or "dev"
        partner_id: res.partner ID

    Returns:
        Dict with id, name, display_name, vat, country_code, is_company,
        email, phone, street, city, zip, customer/supplier rank.
    """
    client = get_client(instance)
    fields = [
        "id", "name", "display_name", "vat", "country_id", "is_company",
        "email", "phone", "street", "city", "zip",
        "customer_rank", "supplier_rank",
    ]
    rows = client.execute_kw("res.partner", "read", [partner_id], {"fields": fields})
    if not rows:
        raise ValueError(f"Partner {partner_id} not found on {instance}")
    _resolve_country_codes(client, rows)
    return rows[0]


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
    domain: list[Any] = [("date", ">=", date_from), ("date", "<=", date_to)]
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
def odoo_search_journal_entries(
    instance: Instance,
    date_from: str | None = None,
    date_to: str | None = None,
    ref: str | None = None,
    state: str | None = None,
    journal_code: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search account.move with `move_type='entry'` (manual journal entries).

    Useful for finding e.g. period-end VAT bookings, corrections, opening
    balances — anything that isn't a standard invoice/bill.

    Args:
        instance: "prod" or "dev"
        date_from: optional YYYY-MM-DD inclusive
        date_to: optional YYYY-MM-DD inclusive
        ref: optional ilike match against the move reference
        state: optional, "draft" or "posted"
        journal_code: optional filter on journal short code (e.g. "MISC")
        limit: max results (default 50)
    """
    client = get_client(instance)
    domain: list[Any] = [("move_type", "=", "entry")]
    if date_from:
        domain.append(("date", ">=", date_from))
    if date_to:
        domain.append(("date", "<=", date_to))
    if ref:
        domain.append(("ref", "ilike", ref))
    if state:
        domain.append(("state", "=", state))
    if journal_code:
        domain.append(("journal_id.code", "=", journal_code))
    fields = ["id", "name", "ref", "date", "state", "journal_id"]
    return client.execute_kw(
        "account.move", "search_read", [domain],
        {"fields": fields, "limit": limit, "order": "date desc, id desc"},
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
    # Resolve account → code, tags → codes (batched)
    acc_ids = list({line["account_id"][0] for line in line_data if line.get("account_id")})
    acc_map: dict[int, str] = {}
    if acc_ids:
        accs = client.execute_kw(
            "account.account", "read", [acc_ids], {"fields": ["id", "code"]}
        )
        acc_map = {a["id"]: a["code"] for a in accs}

    tag_ids = list({tid for line in line_data for tid in (line.get("tax_tag_ids") or [])})
    tag_map: dict[int, str] = {}
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
        account_code: account code, e.g. "2611" or "6231"
        date_from: optional start date inclusive
        date_to: optional end date inclusive

    Returns:
        {account_code, account_name, debit_sum, credit_sum, balance, line_count}
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


@mcp.tool()
def odoo_query_account_aggregate(
    instance: Instance,
    account_codes: list[str],
    date_from: str,
    date_to: str,
    state: str = "posted",
) -> list[dict[str, Any]]:
    """Aggregate debit/credit per account across multiple accounts in a period.

    Generic building block for client-side reporting. Returns one row per
    account code, with `debit_sum`, `credit_sum`, and `balance` (= debit-credit).

    Args:
        instance: "prod" or "dev"
        account_codes: list of account codes to aggregate, e.g. ["2611", "2614", "2641"]
        date_from: YYYY-MM-DD inclusive
        date_to: YYYY-MM-DD inclusive
        state: "posted" (default) or "draft" — applied to the parent move

    Returns:
        List of {account_code, account_name, debit_sum, credit_sum, balance, line_count},
        ordered by account_code. Accounts not found or with no activity are still
        returned with zeros (so callers can rely on a stable result shape).
    """
    if not account_codes:
        return []
    client = get_client(instance)
    accs = client.execute_kw(
        "account.account", "search_read",
        [[("code", "in", account_codes)]],
        {"fields": ["id", "code", "name"]},
    )
    by_code = {a["code"]: a for a in accs}
    acc_ids = [a["id"] for a in accs]

    # Fetch all lines for all requested accounts in a single query, then
    # bucket them by account_id locally. Avoids the N+1 round-trip pattern
    # that scales badly for callers who hand us many account codes.
    lines: list[dict[str, Any]] = []
    if acc_ids:
        domain: list[Any] = [
            ("account_id", "in", acc_ids),
            ("parent_state", "=", state),
            ("date", ">=", date_from),
            ("date", "<=", date_to),
        ]
        lines = client.execute_kw(
            "account.move.line", "search_read", [domain],
            {"fields": ["account_id", "debit", "credit"]},
        )

    buckets: dict[int, dict[str, float | int]] = {
        aid: {"debit": 0.0, "credit": 0.0, "count": 0} for aid in acc_ids
    }
    for line in lines:
        aid = line["account_id"][0]  # [id, name] tuple from XML-RPC
        bucket = buckets.get(aid)
        if bucket is None:
            continue  # shouldn't happen given the domain, but be defensive
        bucket["debit"] += line["debit"]
        bucket["credit"] += line["credit"]
        bucket["count"] += 1

    results = []
    for code in account_codes:
        acc = by_code.get(code)
        if acc is None:
            results.append({
                "account_code": code,
                "account_name": None,
                "debit_sum": 0.0,
                "credit_sum": 0.0,
                "balance": 0.0,
                "line_count": 0,
            })
            continue
        bucket = buckets[acc["id"]]
        debit = float(bucket["debit"])
        credit = float(bucket["credit"])
        results.append({
            "account_code": acc["code"],
            "account_name": acc["name"],
            "debit_sum": round(debit, 2),
            "credit_sum": round(credit, 2),
            "balance": round(debit - credit, 2),
            "line_count": int(bucket["count"]),
        })
    return sorted(results, key=lambda r: r["account_code"])
