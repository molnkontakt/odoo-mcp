"""Read-access policy for the generic read escape hatches.

`odoo_search_read` / `odoo_read_group` / `odoo_fields_get` take the model name
as a free string from the caller, so without a policy they reach everything the
Odoo user can see — which is much more than accounting.

Odoo's own ACL is the first line and already blocks `ir.config_parameter`,
`ir.mail_server`, `ir.logging` and `ir.model` for the MCP service user. It does
*not* block `res.users`, `res.partner.bank` or `mail.message`, and it does not
stop `ir.attachment` from handing out the fields that make a document
retrievable without a session. This module is the second line.

Policy: **default-deny on models, always-deny on fields.**

The field denylist matters independently of the model list: `ir.attachment` is
legitimately used by `odoo_upload_attachment`, but `access_token` makes
`/web/content/<id>?access_token=…` fetchable with no session at all, and
`datas`/`raw` are the document bytes. Those are stripped from results even when
the caller did not name them, because omitting `fields` makes Odoo return its
default set.

**Per-instance extra read models.** An instance can list further models its
generic readers may reach with ``ODOO_<NAME>_EXTRA_READ_MODELS`` (see
``instances.extra_read_models``) — for a custom module that only exists in that
database. The list is exact names only, it only widens `check_model` (which
only the read escape hatches call), and every denial below is checked first.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

#: Model prefixes that are in-domain for an accounting server. Trailing dot is
#: significant: "account." matches "account.move" but not "accountancy.foo".
ALLOWED_MODEL_PREFIXES: tuple[str, ...] = (
    "account.",
    "product.",
    "uom.",
)

#: Exact model names allowed on top of the prefixes above.
ALLOWED_MODELS: frozenset[str] = frozenset(
    {
        "account",
        "res.partner",
        "res.country",
        "res.country.state",
        "res.currency",
        "res.currency.rate",
        "res.company",
        "ir.attachment",
        # Expense claims are accounting data; hr.employee stays closed (see
        # tools/expenses.py for the curated lookup).
        "hr.expense",
        # Calendar and To-do: readable here so a created record can be looked
        # up; every write goes through the curated tools in
        # tools/calendar_events.py and tools/todos.py, which refuse the cases
        # those tools are not meant for (synced events, project tasks).
        # project.tags is read-only everywhere: no tool creates tags.
        "calendar.event",
        "project.task",
        "project.tags",
    }
)

#: Models `odoo_upload_attachment` may attach a file to (and, with
#: `set_as_main`, write `message_main_attachment_id` on). Exact names only:
#: the tool writes to the target record, so it must not take a free model
#: name. Kept apart from ALLOWED_MODELS on purpose — being readable is not a
#: reason to be writable.
UPLOAD_TARGET_MODELS: frozenset[str] = frozenset(
    {
        "account.move",     # supplier bills, invoices, journal entries
        "account.payment",  # remittance advice, payment confirmations
        "hr.expense",       # receipts
        "calendar.event",   # agenda, minutes
        "project.task",     # to-dos only; the tool checks that (tools/todos.py)
    }
)

#: Explicit denials. Checked BEFORE the allow rules so a future prefix change
#: cannot silently open one of these up.
DENIED_MODELS: frozenset[str] = frozenset(
    {
        "res.users",
        "res.groups",
        "res.partner.bank",
        "mail.message",
        "mail.followers",
        "ir.config_parameter",
        "ir.mail_server",
        "ir.logging",
        "ir.model",
        "ir.model.access",
        "ir.rule",
        "auth.totp.device",
    }
)

#: Namespaces a per-instance extra list may never name, on top of
#: DENIED_MODELS: framework administration, users/groups and their satellites,
#: messaging and authentication. The base allow rules never reach these either
#: (``ir.attachment`` is the one ``ir.*`` model, and it is allowed by name).
EXTRA_DENIED_PREFIXES: tuple[str, ...] = (
    "ir.",
    "res.users.",
    "res.groups.",
    "mail.",
    "auth.",
    "auth_",
    "bus.",
    "base.",
)

#: Exact Odoo model name: lower-case dotted identifiers, no wildcards.
MODEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

#: Fields never returned by the generic readers, on any model. These are the
#: ones that turn a read permission into document exfiltration.
DENIED_FIELDS: frozenset[str] = frozenset(
    {
        "datas",
        "raw",
        "db_datas",
        "store_fname",
        "access_token",
        "password",
        "password_crypt",
        "new_password",
        "signature",
    }
)


class AccessDenied(Exception):
    """Raised when the policy blocks a model or field."""


def is_denied_model(name: str) -> bool:
    """True for models no configuration may open (the extra-list denylist)."""
    return name in DENIED_MODELS or any(name.startswith(p) for p in EXTRA_DENIED_PREFIXES)


def parse_extra_read_models(raw: str | None, source: str) -> frozenset[str]:
    """Parse a comma-separated list of exact model names.

    Strict on purpose, because a typo here is a policy decision: entries are
    trimmed and empty ones ignored, but anything that is not an exact model
    name (wildcards, prefixes, upper case) or that names a denied model raises
    ValueError. ``source`` names the variable in the error message.
    """
    names: set[str] = set()
    for part in (raw or "").split(","):
        name = part.strip()
        if not name:
            continue
        if not MODEL_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{source}: {name!r} is not an exact Odoo model name "
                f"(expected e.g. 'acme.budget'; wildcards and prefixes are not accepted)."
            )
        if is_denied_model(name):
            raise ValueError(
                f"{source}: {name!r} is denied by policy and cannot be opened by configuration."
            )
        names.add(name)
    return frozenset(names)


def check_model(model: str, *, extra_read_models: Collection[str] = ()) -> None:
    """Raise AccessDenied unless `model` is readable via the generic tools.

    ``extra_read_models`` is the per-instance extra list for the instance the
    call goes to. Only the read escape hatches (tools/read.py) call this; write
    tools address fixed models and never consult it. Denials are checked first,
    so the extra list can never open a denied model.
    """
    name = (model or "").strip()
    if not name:
        raise AccessDenied("No model given.")

    if name in DENIED_MODELS:
        raise AccessDenied(
            f"Model '{name}' is blocked by policy. It holds credentials, "
            f"personal data or bank details that are out of scope for this "
            f"accounting server, and no curated tool needs it."
        )

    if name in ALLOWED_MODELS:
        return
    if any(name.startswith(p) for p in ALLOWED_MODEL_PREFIXES):
        return
    if name in extra_read_models and not is_denied_model(name):
        return

    raise AccessDenied(
        f"Model '{name}' is not in the allowed accounting domain. "
        f"Allowed: {', '.join(sorted(ALLOWED_MODEL_PREFIXES))}* plus "
        f"{', '.join(sorted(ALLOWED_MODELS))}"
        f"{''.join(', ' + m for m in sorted(extra_read_models))}. "
        f"If this model is genuinely needed on one instance, list it in "
        f"ODOO_<NAME>_EXTRA_READ_MODELS for that instance (read-only); for "
        f"every instance, add it to ALLOWED_MODELS in odoo_mcp/access.py — "
        f"deliberately, not at call time."
    )


def check_upload_target(model: str) -> None:
    """Raise AccessDenied unless `odoo_upload_attachment` may write to `model`."""
    name = (model or "").strip()
    if name not in UPLOAD_TARGET_MODELS:
        raise AccessDenied(
            f"Attachments can only be uploaded to {', '.join(sorted(UPLOAD_TARGET_MODELS))}; "
            f"got {name or 'no model'!r}. The upload also writes the target record "
            f"(set_as_main), so the list is fixed in odoo_mcp/access.py."
        )


def check_fields(fields: list[str] | None) -> None:
    """Raise AccessDenied if the caller explicitly asked for a denied field."""
    if not fields:
        return
    bad = sorted({f for f in fields if f.split(":")[0] in DENIED_FIELDS})
    if bad:
        raise AccessDenied(
            f"Field(s) {bad} are blocked by policy. Attachment payloads and "
            f"access tokens are not readable through the generic tools — an "
            f"access_token alone makes a document fetchable without a session."
        )


def scrub_row(row: dict[str, Any]) -> dict[str, Any]:
    """Drop denied keys from one result row."""
    return {k: v for k, v in row.items() if k not in DENIED_FIELDS}


def scrub_rows(rows: Any) -> Any:
    """Drop denied keys from a result set.

    Applied even when the caller named no fields, because Odoo then returns its
    default set — which for ir.attachment includes the payload fields.
    """
    if isinstance(rows, list):
        return [scrub_row(r) if isinstance(r, dict) else r for r in rows]
    return rows
