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
"""

from __future__ import annotations

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


def check_model(model: str) -> None:
    """Raise AccessDenied unless `model` is readable via the generic tools."""
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

    raise AccessDenied(
        f"Model '{name}' is not in the allowed accounting domain. "
        f"Allowed: {', '.join(sorted(ALLOWED_MODEL_PREFIXES))}* plus "
        f"{', '.join(sorted(ALLOWED_MODELS))}. "
        f"If this model is genuinely needed, add it to ALLOWED_MODELS in "
        f"odoo_mcp/access.py — deliberately, not at call time."
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
