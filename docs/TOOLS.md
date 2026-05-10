# Tools

Reference for every MCP tool exposed by `odoo-mcp`.

## Common parameters

All tools take `instance: "prod" | "dev"` as the first parameter. The
server resolves the URL/credentials from environment variables prefixed
with the instance name in uppercase (e.g. `ODOO_PROD_URL`).

## Read tools (Phase 1, shipped)

### `odoo_search_partners(instance, query, limit=20)`

Search `res.partner` by name or VAT (case-insensitive substring match).

**Returns:** list of `{id, name, vat, country_code, is_company}`.
`country_code` is always present (None when the partner has no country set).

### `odoo_get_partner(instance, partner_id)`

Get full info for one partner.

**Returns:** `{id, name, display_name, vat, country_code, is_company,
email, phone, street, city, zip, customer_rank, supplier_rank}`.

### `odoo_search_invoices(instance, date_from, date_to, move_type?, state?, partner_id?, limit=50)`

Search `account.move` (invoices and journal entries) within a date range.

- `move_type`: optional, e.g. `"in_invoice"`, `"out_invoice"`, `"entry"`
- `state`: optional, `"draft"` or `"posted"`
- `partner_id`: optional partner filter

**Returns:** list of header fields per move (id, name, ref, date, state,
move_type, partner_id, amount_total, amount_residual, currency_id).

### `odoo_search_journal_entries(instance, date_from?, date_to?, ref?, state?, journal_code?, limit=50)`

Search `account.move` filtered to `move_type='entry'` (manual journal entries).

Useful for finding period-end VAT bookings, corrections, opening balances —
anything that isn't a standard invoice.

**Returns:** list of `{id, name, ref, date, state, journal_id}`.

### `odoo_get_invoice(instance, move_id)`

Get full `account.move` with all journal lines resolved.

**Returns:** header dict plus a `lines` array. Each line:
`{id, name, account_code, debit, credit, partner_id, tax_tag_codes}`.

### `odoo_get_account_balance(instance, account_code, date_from?, date_to?)`

Sum `debit - credit` on `account.move.line` for a given account code,
restricted to posted moves.

> Uses `debit - credit` rather than the cached `balance` field — `balance`
> can drift from the authoritative debit/credit values for foreign-currency
> invoices in some Odoo versions.

**Returns:** `{account_code, account_name, debit_sum, credit_sum, balance, line_count}`.

### `odoo_query_account_aggregate(instance, account_codes, date_from, date_to, state="posted")`

Aggregate debit/credit per account across multiple accounts in a period.

**Returns:** list of `{account_code, account_name, debit_sum, credit_sum,
balance, line_count}`, ordered by `account_code`. Accounts not found or
without activity still appear in the result with zeros, so callers can
rely on a stable result shape.

## Write tools — safe (Phase 2, shipped)

These tools never post or commit data the user can't easily reverse — they
create drafts only. No confirmation flag is required. Every call is audit-logged
when `MCP_AUDIT_DB_URL` is set; otherwise audit-logging is a silent no-op.

All payloads run through validators (`BalanceValidator`,
`AccountsExistValidator`, `TaxTagsExistValidator`, plus any plugins loaded
via `MCP_VALIDATORS_PATH`) before reaching Odoo.

### `odoo_create_journal_entry_draft(instance, date, lines, ref?, journal_code?)`

Create an `account.move` in `draft` state.

- `date`: YYYY-MM-DD
- `lines`: list of dicts with `account_code` (required), `debit` (default 0),
  `credit` (default 0), `name?`, `tax_tag_codes?` (e.g. `["se_30"]`),
  `partner_id?`
- `ref`: optional reference / description
- `journal_code`: optional journal short code; defaults to the first
  `general` (Misc) journal

**Returns:** `{move_id, name, state, line_count}`.

**Validates:** `sum(debit) == sum(credit)`, all `account_code`s exist on
the instance, all `tax_tag_codes` exist on the instance.

### `odoo_set_partner(instance, move_id, partner_id)`

Set the partner on a draft `account.move`. Rejected on posted moves.

**Returns:** `{move_id, partner_id, partner_name}`.

### `odoo_add_tax_tags(instance, line_id, tag_codes, replace=False)`

Add or replace tax tags on a single `account.move.line`. Only works while
the parent move is in draft state — Odoo locks tags on posted moves.

- `tag_codes`: list of tag short codes, e.g. `["se_30", "se_48"]`
- `replace`: if True, overwrite existing tags. Default False (additive).

**Returns:** `{line_id, applied_tags}`.

## Write tools — critical (Phase 3, planned)

Not yet implemented. Will require `confirm=True` in payload + extended
validation (period locks, etc.). See [ARCHITECTURE.md](ARCHITECTURE.md#tool-catalog-planned).

Planned: `odoo_post_journal_entry`, `odoo_register_payment`.
