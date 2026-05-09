# Tools

Reference for every MCP tool exposed by `odoo-mcp`.

> **Status:** Phase 1 (read-only) tools are implemented. Write tools are
> documented as planned but not yet shipped.

## Common parameters

All tools take `instance: "prod" | "dev"` as the first parameter. The
server resolves the URL/credentials from environment variables prefixed
with the instance name in uppercase (e.g. `ODOO_PROD_URL`).

## Read tools

### `odoo_search_partners(instance, query, limit=20)`

Search `res.partner` by name or VAT (case-insensitive substring match).

**Returns:** list of `{id, name, vat, country_code, is_company}`.

### `odoo_search_invoices(instance, date_from, date_to, move_type?, state?, partner_id?, limit=50)`

Search `account.move` (invoices and journal entries) within a date range.

- `move_type`: optional filter, e.g. `"in_invoice"`, `"out_invoice"`, `"entry"`
- `state`: optional, `"draft"` or `"posted"`
- `partner_id`: optional partner filter

**Returns:** list of header fields per move (id, name, ref, date, state,
move_type, partner_id, amount_total, amount_residual, currency_id).

### `odoo_get_invoice(instance, move_id)`

Get full `account.move` with all journal lines resolved.

**Returns:** header dict plus a `lines` array. Each line has:
`{id, name, account_code, debit, credit, partner_id, tax_tag_codes}`.

### `odoo_get_account_balance(instance, account_code, date_from?, date_to?)`

Sum `debit - credit` on `account.move.line` for a given account code,
restricted to posted moves.

> Uses `debit - credit` rather than the cached `balance` field — `balance`
> can drift from the authoritative debit/credit values for foreign-currency
> invoices in some Odoo versions.

**Returns:** `{account_code, account_name, debit_sum, credit_sum, balance, line_count}`.

## Write tools (planned, Phase 2/3)

See [ARCHITECTURE.md](ARCHITECTURE.md#tool-catalog-planned) for the full
planned catalog.
