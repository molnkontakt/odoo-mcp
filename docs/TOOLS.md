# Tools

Reference for every MCP tool exposed by `odoo-mcp`.

> **Multi-company reads.** Search results carry `company_id`; move names (`BNK1/2026/0014`) and account codes repeat per company. `odoo_get_account_balance` / `odoo_query_account_aggregate` refuse an account code that exists in several companies unless `company_id` is given.

> `instance` is the last, optional parameter of every tool: it may be omitted when the server has a single configured instance.

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

### `odoo_search_invoices(instance, date_from, date_to, move_type?, state?, partner_id?, limit=50, company_id?)`

Search `account.move` (invoices and journal entries) within a date range.

- `move_type`: optional, e.g. `"in_invoice"`, `"out_invoice"`, `"entry"`
- `state`: optional, `"draft"` or `"posted"`
- `partner_id`: optional partner filter

**Returns:** list of header fields per move (id, name, ref, date, state,
move_type, partner_id, amount_total, amount_residual, currency_id).

### `odoo_search_journal_entries(instance, date_from?, date_to?, ref?, state?, journal_code?, limit=50, company_id?)`

Search `account.move` filtered to `move_type='entry'` (manual journal entries).

Useful for finding period-end VAT bookings, corrections, opening balances —
anything that isn't a standard invoice.

**Returns:** list of `{id, name, ref, date, state, journal_id}`.

### `odoo_get_invoice(instance, move_id)`

Get full `account.move` with all journal lines resolved.

**Returns:** header dict plus a `lines` array. Each line:
`{id, name, account_code, debit, credit, partner_id, tax_tag_codes}`.

### `odoo_get_account_balance(instance, account_code, date_from?, date_to?, company_id?)`

Sum `debit - credit` on `account.move.line` for a given account code,
restricted to posted moves.

> Uses `debit - credit` rather than the cached `balance` field — `balance`
> can drift from the authoritative debit/credit values for foreign-currency
> invoices in some Odoo versions.

**Returns:** `{account_code, account_name, debit_sum, credit_sum, balance, line_count}`.

### `odoo_query_account_aggregate(instance, account_codes, date_from, date_to, state="posted", company_id?)`

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
> `res.company`, `ir.attachment`, `hr.expense`, `calendar.event`,
> `project.task`, `project.tags`. Anything else fails closed — including models
> introduced later by a new Odoo module. Being readable here opens no write:
> the calendar and to-do models are written only by their curated tools.
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
> To widen the domain for every instance, edit `ALLOWED_MODELS` in
> `odoo_mcp/access.py` deliberately. There is no call-time override.
>
> **Per-instance extra read models:** an instance with a custom module can let
> these three tools reach its models with `ODOO_<NAME>_EXTRA_READ_MODELS`, a
> comma-separated list of exact model names (e.g.
> `ODOO_ACME_EXTRA_READ_MODELS=acme.budget,acme.budget.line`).
>
> - **Exact names only** — no wildcards or prefixes; each entry must match
>   `^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$`. Entries are trimmed, empty ones ignored.
> - **Denials win.** A denied model, or anything under `ir.`, `res.users.`,
>   `res.groups.`, `mail.`, `auth.`/`auth_`, `bus.` or `base.`, cannot be listed.
>   The server refuses to start on an invalid or denied entry.
> - **Bound to the instance** it is set for; other instances still refuse the model.
> - **Read-only.** Only the generic readers consult it. No write tool, and not
>   the attachment tools, is opened by it. Denied fields are still refused and
>   scrubbed on these models.

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
| `odoo_overdue_invoices(instance, company_id?, as_of?, min_days_overdue=0, partner_id?, limit=200)` | posted customer invoices past due with a balance: days overdue, last/next reminder level (when `account_invoice_reminder` is installed) |
| `odoo_unpaid_by_customer(instance, company_id?, include_not_due=True, limit=500)` | open customer balances grouped per customer, overdue first |
| `odoo_unreconciled_bank_lines(instance, company_id?, journal_code?, date_from?, limit=200)` | bank statement lines still unreconciled, net per journal |
| `odoo_customer_statement(instance, partner_id, company_id?, include_paid=False, limit=100)` | one customer's invoices/credit notes and balance due |
| `odoo_list_attachments(res_model, res_id)` | files on an accounting/expense record: `attachment_id, name, mimetype, file_size, viewable` (no content) |
| `odoo_get_attachment_image(attachment_id, page=1, max_px=1600, company_id?)` | one attachment as an inline image: images downscaled to JPEG, PDFs rendered one page per call as PNG; only image/PDF on account.*/hr.expense*/product.*/res.partner. Needs the `images` extra (Pillow, PyMuPDF). Internal users only: Odoo gives portal users no RPC access to `ir.attachment` |
| `odoo_list_employees(query?, company_id?, limit=50)` | employees an expense can be filed for: `employee_id, name, company, work_email` — one row per company the person belongs to; read from `hr.employee.public`, so any internal user can call it, and nothing private from `hr.employee` is served |
| `odoo_list_expense_categories(company_id?)` | expense categories (products flagged *can be expensed*): `product_id, code, name, company`; `code` is what `odoo_create_expense` takes |
| `odoo_list_companies(instance)` | companies: `id, name, currency_id` — journals and accounts are per company |
| `odoo_list_journals(instance, company_id?)` | journals: `id, code, name, type, company_id` |
| `odoo_list_accounts(instance, query?, account_type?, limit=200, company_id?)` | CoA: `id, code, name, account_type, company_ids` |
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

### `odoo_create_journal_entry_draft(instance, date, lines, ref?, journal_code?, company_id?)`

Multi-company: `journal_code` and account codes are resolved within `company_id`; without it,
a code that exists in several companies is rejected rather than picked silently.

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

### `odoo_create_invoice(instance, move_type, partner_id, lines, invoice_date?, ref?, journal_code?, company_id?)`

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

### `odoo_upload_attachment(instance, res_model, res_id, filename, data_base64, mimetype?, set_as_main=False)`

Attach a base64-encoded file to a record (e.g. a supplier PDF onto a draft
bill — feeds the OCR flow). `set_as_main=True` also makes it the record's main
attachment (`message_main_attachment_id`), which Odoo otherwise only does for
chatter uploads — use it for a receipt added after the expense was created.
Photos: downscale first (JPEG, ≤1600 px on the long side, quality ~80).

- `res_model` must be one of `account.move`, `account.payment`, `hr.expense`,
  `calendar.event`, `project.task` (`UPLOAD_TARGET_MODELS` in
  `odoo_mcp/access.py`). Anything else is refused before Odoo is called: with
  `set_as_main` the tool writes the target record, so a free model name would
  be a generic write.
- `project.task` only for to-dos (no project, no parent task), the same
  boundary as the to-do tools.
- `set_as_main` needs Odoo's main-attachment field on the target. Odoo 19 has
  it on invoices and expenses but not on calendar events or tasks; there the
  call is refused before anything is uploaded.

**Returns:** `{attachment_id, name, res_model, res_id, main_attachment}`.

### `odoo_create_expense(instance, employee_id, name, total_amount, date, category_code?, product_id?, receipt_base64?, receipt_filename?, receipt_mimetype?, payment_mode="own_account", description?)`

Create a **draft** expense claim (`hr.expense`) — the treasurer filing a board
member's receipt, or an employee filing their own. Odoo's record rules decide
which: a plain employee can only create on their own employee record, an
expense approver on anyone's. The company is taken from the
employee record, the category must be usable in that company, and the receipt
travels in the same call: it is attached and set as the expense's main
attachment, so the voucher never exists without its document. Nothing is
submitted, approved or posted.

- `category_code`: the category's internal reference (`odoo_list_expense_categories`), or `product_id`
- `total_amount`: VAT included, company currency
- `payment_mode`: `own_account` (reimburse the employee, default) | `company_account`
- `receipt_base64`: downscale a photo first (JPEG, ≤1600 px on the long side,
  quality ~80 → 100–300 kB); a raw phone photo is too large as a tool argument.
  Without a receipt the result carries a `warning` and the voucher is incomplete
  until one is added with `odoo_upload_attachment(..., set_as_main=True)`.
- Needs the Expenses app (`hr_expense`); on an instance without it the three
  expense tools answer "module not installed" before touching anything.

**Returns:** `{expense_id, name, state, employee, company, category, total_amount, currency, date, payment_mode, attachment_id}`.

## Calendar and To-do

Curated tools for Odoo's Calendar (`calendar.event`) and To-do app (a
`project.task` without a project). The write tools are `odoo:write` tier (plus
`odoo:prod` on a production instance, as for every tool); `odoo_list_todos` is
read tier. Writes are audit-logged like the other write tools.

> [!important] Quiet by default
> Odoo mails attendees when an event is created or moved, and mails a user who
> is assigned a task. These tools do neither unless asked:
>
> - **Calendar**, `notify=False` (default): the write carries the context keys
>   `no_mail_to_attendees`, `skip_attendee_notification`, `dont_notify` and
>   `mail_create_nolog`, and no reminders (`alarm_ids`) are set. `notify=True`
>   leaves Odoo's behaviour alone: invitations on create, "date updated" on a
>   new time (future events only).
> - **To-do**, always: `mail_auto_subscribe_no_notify` (no "you have been
>   assigned" mail), `mail_create_nolog` and `mail_notrack` (no tracking
>   messages). Assignees still become followers, which sends nothing by itself.
>
> Verified against Odoo 19 in a throwaway database: no `mail.mail` and no e-mail
> notification for any quiet call, assignment to another user included, while
> the `notify=True` and no-context controls each produced the expected mails.

**Times.** A time without an offset is wall-clock time in `timezone` (IANA name,
default `Europe/Stockholm`, daylight saving handled); `2026-10-05T10:00:00+02:00`
or `…Z` is taken as given. Odoo stores UTC; results show local time. An all-day
event follows Odoo's own convention (`start_date`/`stop_date`, 08:00–18:00).

### `odoo_create_calendar_event(name, date?, end_date?, start?, stop?, timezone="Europe/Stockholm", description?, location?, attendee_partner_ids?, notify=False)`

All-day (`date`, optional `end_date`) or timed (`start` and `stop`).
Without `attendee_partner_ids` Odoo makes the caller's contact the only
attendee, as in its UI; with it, the list is exactly those `res.partner` ids
(include your own to be on it). Plain-text `description` keeps its line breaks.

**Returns:** `{event_id, name, allday, start, stop, timezone, location, organizer, attendees, recurring, active, notified, summary}`.

### `odoo_update_calendar_event(event_id, name?, date?, end_date?, start?, stop?, timezone=…, description?, location?, attendee_partner_ids?, notify=False, recurrence_update="self_only")`

Only the given fields change; `start` alone moves a timed event and keeps its
length; `attendee_partner_ids` replaces the list; `""` clears `description` or
`location`. `recurrence_update` (`self_only` | `future_events` | `all_events`)
applies to events in a recurring series. **Returns:** the event plus `updated`.

### `odoo_archive_calendar_event(event_id, recurrence_update="self_only")`

Sets `active=False` — never deletes; the event stays restorable from Odoo's
*Archived* filter. For a series it uses Odoo's own `action_mass_archive`, so
`future_events` trims the recurrence as the UI does.
**Returns:** `{event_id, name, archived, recurrence_update, archived_count}`.

> [!warning] Synchronised events are refused
> When the database has the field `l10n_se_cc_key` on `calendar.event` (set by
> a calendar synchronisation module) and it is set, update and archive refuse:
> the change would be overwritten by the next sync. For `future_events` /
> `all_events` every event in the series is checked. The field is looked up with
> `fields_get` per call, so the tools behave the same where the module is absent.

### `odoo_list_todos(mine=True, status="open", query?, limit=50, timezone=…)`

To-dos (no project, no parent), ordered like the To-do app. `mine` keeps the
ones assigned to the caller — with act-as-caller that is the human, worked out
from Odoo itself; `mine=False` lists every to-do Odoo lets the caller see.
`status`: `open` (anything not done or cancelled) | `done` | `all`.
**Returns:** `[{todo_id, name, state, done, deadline, deadline_at, priority, tags, assignees, active}]`.

### `odoo_create_todo(name, description?, deadline?, user_ids?, tags?, priority?, timezone=…)`

- `user_ids` (res.users ids): default is the caller, as in the To-do app. Odoo
  always adds the caller to a new to-do, so its creator keeps seeing it.
- `deadline`: `YYYY-MM-DD` (end of that day in `timezone`) or a date and time.
- `tags`: names of **existing** `project.tags`, matched case-insensitively.
  Unknown names are refused, never created — an assistant inventing tags would
  litter the tag list.
- `priority`: `0` (normal) … `3` (urgent); the To-do app shows `1` as a star.

**Returns:** the to-do as in `odoo_list_todos`.

### `odoo_update_todo(todo_id, name?, description?, deadline?, user_ids?, tags?, priority?, timezone=…)`

Only the given fields change; `user_ids` and `tags` replace the lists; `""`
clears `description` or `deadline`. Removing yourself from `user_ids` can hide
the to-do from you (Odoo shows a private task only to its assignees).
**Returns:** the to-do plus `updated`.

### `odoo_set_todo_state(todo_id, done)`

`done=True` sets `state="1_done"`, `done=False` sets `"01_in_progress"` — the
values Odoo 17+ uses (`project.task.state`; `1_done`/`1_canceled` are the
closed states). Re-opening a to-do that is already open changes nothing.
**Returns:** `{todo_id, name, previous_state, state, done, changed}`.

> [!note] Only to-dos
> Every to-do tool reads the task first and refuses one with a project or a
> parent task: project tasks have stages, customers and followers, and are out
> of scope. On an instance without the To-do app (`project_todo`) or without
> Calendar (`calendar`) the tools answer "module not installed" before touching
> anything.

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
