# Tools

Reference for every MCP tool exposed by `odoo-mcp`.

## Common parameters

All tools take `instance: "prod" | "dev"` as the first parameter. The
server resolves the URL/credentials from environment variables prefixed
with the instance name in uppercase (e.g. `ODOO_PROD_URL`).

## Required scopes

Only enforced on the HTTP transport with `MCP_AUTH_MODE=oauth`; stdio callers
are trusted by the local process boundary.

| Tier | Scope | Implies |
|---|---|---|
| Read tools | `odoo:read` | — |
| Write — safe | `odoo:write` | `odoo:read` |
| Write — critical | `odoo:critical` | `odoo:write` |
| Any tier with `instance="prod"` | `odoo:prod` (in addition) | — |

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

## Read escape-hatches + metadata (Phase 1.5, shipped)

Generic read-only tools so an agent can reach the long tail of Odoo without a
bespoke tool per model. All read-tier — they never mutate state.

> [!important] Access policy — default-deny on models, always-deny on fields
> These three tools take the model name as a free string from the caller, so
> they are constrained by `odoo_mcp/access.py`. Odoo's own ACL is the first
> line and already blocks `ir.config_parameter`, `ir.mail_server`, `ir.logging`
> and `ir.model` for the service user; the policy is the second line.
>
> **Allowed:** `account.*`, `product.*`, `uom.*`, plus `res.partner`,
> `res.country`, `res.country.state`, `res.currency`, `res.currency.rate`,
> `res.company`, `ir.attachment`. Anything else fails closed — including models
> introduced later by a new Odoo module.
>
> **Denied models:** `res.users`, `res.groups`, `res.partner.bank`,
> `mail.message`, `mail.followers`, and the `ir.*` administration models.
> Denials are checked *before* the allow rules, so a future prefix change
> cannot silently open one up.
>
> **Denied fields, on every model:** `datas`, `raw`, `db_datas`, `store_fname`,
> `access_token`, `password*`, `signature`. Naming one raises; they are also
> stripped from results, because omitting `fields` makes Odoo return its
> default set. `access_token` matters most — it makes
> `/web/content/<id>?access_token=…` fetchable with **no session at all**, so a
> read permission would otherwise be enough to lift documents out permanently.
>
> To widen the domain, edit `ALLOWED_MODELS` in `odoo_mcp/access.py`
> deliberately. There is no runtime override.

### `odoo_search_read(instance, model, domain?, fields?, limit=80, offset=0, order?)`

Generic `search_read` against any model. `domain` is a standard Odoo domain
(list of `[field, op, value]` triples + `"|"`/`"&"`/`"!"` operators, implicit
AND). Relational fields come back as `[id, display_name]` pairs (not resolved).

```jsonc
odoo_search_read("dev", "account.move",
  domain=[["move_type","=","out_invoice"],["state","=","posted"]],
  fields=["name","partner_id","amount_total"], limit=20, order="date desc")
```

### `odoo_read_group(instance, model, groupby, fields?, domain?, limit?, orderby?)`

Server-side group + aggregate (Odoo `read_group`, `lazy=False`). Numeric
`fields` are summed per group; each group also carries `__count`. Cheaper than
pulling rows and summing client-side.

```jsonc
odoo_read_group("dev", "account.move.line",
  groupby=["account_id"], fields=["balance"],
  domain=[["parent_state","=","posted"],["date",">=","2026-01-01"]])
```

### `odoo_fields_get(instance, model, attributes?)`

Introspect a model's fields (name → metadata). Default attributes:
`string, type, help, required, readonly, relation, selection`. Use before
`search_read`/write tools on an unfamiliar model.

### Metadata readers

Thin lookups for picking the right code/id when building entries/invoices:

| Tool | Returns |
|------|---------|
| `odoo_list_journals(instance)` | journals: `id, code, name, type` |
| `odoo_list_accounts(instance, query?, account_type?, limit=200)` | CoA: `id, code, name, account_type` |
| `odoo_list_taxes(instance, type_tax_use?)` | taxes: `id, name, amount, amount_type, type_tax_use, price_include` |
| `odoo_list_tax_tags(instance)` | tax-report tags: `id, name` (the `tax_tag_codes` values) |
| `odoo_list_products(instance, query?, limit=50)` | products: `id, name, default_code, list_price, uom_id` |

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

### `odoo_create_invoice(instance, move_type, partner_id, lines, invoice_date?, ref?, journal_code?)`

Create a **draft** customer/vendor invoice or refund. Odoo computes the tax
lines + totals from each line's taxes — the correct way to make a VAT-bearing
document (vs a raw journal entry).

- `move_type`: `out_invoice` | `in_invoice` | `out_refund` | `in_refund`
- `lines`: `[{name, price_unit, quantity=1, account_code?, tax_names?, product_id?}]`
- `tax_names` **must match the direction** (sale for `out_*`, purchase for `in_*`) —
  a wrong-direction tax is rejected before creation so incorrect VAT can't reach
  the momsrapport.

**Returns:** `{move_id, name, state, move_type, amount_untaxed, amount_tax, amount_total, line_count}`.

### `odoo_update_invoice(instance, move_id, values)`

Update a whitelist of header fields on a **draft** move (`partner_id`,
`invoice_date`, `invoice_date_due`, `ref`, `narration`, `payment_reference`).
Rejected on posted moves and for any other field. **Returns:** `{move_id, updated}`.

### `odoo_create_partner(instance, name, is_company=True, vat?, email?, phone?, street?, city?, zip_code?, country_code?, customer=False, supplier=False)`

Create a `res.partner`. `country_code` (e.g. `"SE"`) is resolved to `country_id`;
`customer`/`supplier` set the respective rank. **Returns:** `{partner_id, name}`.

### `odoo_create_product(instance, name, list_price=0, default_code?, product_type="service", sale_ok=True, purchase_ok=False)`

Create a `product.product` (`product_type`: `service` | `consu`).
**Returns:** `{product_id, name}`.

### `odoo_upload_attachment(instance, res_model, res_id, filename, data_base64, mimetype?)`

Attach a base64-encoded file to any record (e.g. a supplier PDF onto a draft
bill — feeds the OCR flow). **Returns:** `{attachment_id, name, res_model, res_id}`.

## Write tools — critical (Phase 3, shipped)

Tools that change posted state and can move money. They follow the same
rules:

- **`confirm=True`** is required to actually do the work. Without it the
  tool returns a preview/dry-run summary and runs the post-time validator
  chain so the LLM (and the user reading the transcript) can sanity-check.
- **`idempotency_key`** is optional but recommended in production. If
  audit-log is enabled and a successful prior call exists with that key,
  the tool returns the previous summary instead of re-acting. Lets you
  safely retry transient transport errors.
- The post-time validator chain runs **before** the actual write
  (`PostStateValidator`, `PostBalanceValidator`, plus any plugins).

### `odoo_post_journal_entry(instance, move_id, confirm=False, idempotency_key=None)`

Promote a draft `account.move` to `posted` state.

- `confirm=False` → returns `{preview: True, validators_passed: True, ...summary}`
- `confirm=True` → returns `{posted: True, replayed: bool, ...summary}`

**Validates:** move exists, currently in `draft` state, balanced.

### `odoo_register_payment(instance, move_id, journal_code, amount, payment_date=None, confirm=False, idempotency_key=None)`

Register a payment against a posted invoice via Odoo's
`account.payment.register` wizard. Creates the payment row and
reconciles it with the invoice.

- `journal_code`: short code of the bank/cash journal (e.g. `"BNK1"`)
- `amount`: payment amount in the invoice's currency
- `payment_date`: YYYY-MM-DD; defaults to the invoice date — and the write
  path books exactly the date the preview showed. (Odoo's wizard defaults to
  *today*, so this is resolved explicitly rather than left to the wizard.)

- `confirm=False` → preview with the invoice summary + journal info
- `confirm=True` → returns `{registered: True, payment_ids: [...], ...}`

**Validates:** invoice in `posted` state, journal exists and is type
`bank` or `cash`.

> [!note] No `validators_passed` key
> Unlike `odoo_post_journal_entry`, no validator registry runs for payments or
> reversals, so those previews deliberately omit the key rather than reporting
> a hardcoded `True`.

> [!warning] This does not reconcile the bank statement
> The payment is reconciled against the **invoice**. The corresponding
> `account.bank.statement.line` is untouched and stays unreconciled. There is
> no bank-statement reconciliation tool in this server.

### `odoo_reverse_move(instance, move_id, reason, journal_code=None, date=None, confirm=False, idempotency_key=None)`

Reverse a posted `account.move` via Odoo's `account.move.reversal` wizard.
Creates a new move with the original lines flipped (debit↔credit) and links
it back via `reversed_entry_id`. The original is left untouched so the audit
trail is preserved end-to-end.

- `reason`: short description; appears on the new move's ref
- `journal_code`: optional; defaults to the same journal as the original
- `date`: YYYY-MM-DD; defaults to today (Odoo wizard default)
- `allow_additional_reversal`: default `False`. A move that already has a
  reversal is rejected, because a second one nets the ledger back out while
  leaving two spurious verifications behind.

- `confirm=False` → preview with original-move summary, journal info and
  `existing_reversals`
- `confirm=True` → returns `{reversed: bool, reversal_state: [...], original,
  reversal: [{move_id, name, state, ...}]}`

> [!warning] `reversed: True` only when the reversal is actually posted
> Odoo's `refund_moves()` posts and reconciles the reversal **only** when the
> move's `move_type` is `entry` (a manual journal entry). For invoices, bills
> and refunds it creates the credit note in **draft** and leaves the original
> fully open. This tool reports what happened rather than what was intended:
> `reversed` is `True` only when every created reversal is posted, and
> `reversal_state` carries the per-move states either way. A draft reversal
> still needs `odoo_post_journal_entry` before the correction takes effect.

**Validates:** original move is in `posted` state, and is not already
reversed unless `allow_additional_reversal=True`. Discovers the local
Odoo's `account.move.reversal` field set at runtime so it works across
Odoo 16/17/18/19 even when the wizard schema drifts.

Use cases: undoing accidental posts, issuing credit memos against vendor
bills, reversing a wrong period-end journal entry. In Sweden this is the
correct way to honor BFL 5 kap 5 § (corrections must remain visible
alongside the originals — never overwrite).
