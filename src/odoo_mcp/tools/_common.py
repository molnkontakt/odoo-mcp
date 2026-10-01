"""Small helpers shared by the calendar and to-do tools: times, HTML, relations."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo_mcp.validators import ValidationError

#: Time zone a naive time is read in when the caller names none.
DEFAULT_TZ = "Europe/Stockholm"

#: Odoo's wire format for Datetime fields. Odoo stores them in UTC, naive.
ODOO_DATETIME = "%Y-%m-%d %H:%M:%S"

_HTML_TAG = re.compile(r"<[a-zA-Z/!][^>]*>")


def zone(tz: str | None) -> ZoneInfo:
    name = (tz or DEFAULT_TZ).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValidationError(f"Unknown time zone {name!r}; use an IANA name such as 'Europe/Stockholm'.") from e


def parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as e:
        raise ValidationError(f"{label} must be a date YYYY-MM-DD, got {value!r}") from e


def parse_local_datetime(value: str, tz: str | None, label: str) -> datetime:
    """A caller's datetime as an aware datetime.

    An explicit offset (``2026-10-05T10:00:00+02:00``, ``…Z``) is taken as
    given; a naive one (``2026-10-05 10:00``) is wall-clock time in `tz` —
    daylight saving included, which is the reason not to do this arithmetic
    by hand.
    """
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValidationError(
            f"{label} must be a date and time, e.g. '2026-10-05 10:00' or "
            f"'2026-10-05T10:00:00+02:00'; got {value!r}"
        ) from e
    if len(text) <= 10:
        raise ValidationError(f"{label} needs a time of day as well, got {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone(tz))
    return parsed


def to_odoo_utc(moment: datetime) -> str:
    """Aware datetime → Odoo's naive-UTC string."""
    return moment.astimezone(UTC).strftime(ODOO_DATETIME)


def from_odoo_utc(value: Any, tz: str | None) -> str | None:
    """Odoo's naive-UTC string → ISO 8601 in `tz`, for showing back to the caller."""
    if not value:
        return None
    moment = datetime.strptime(str(value), ODOO_DATETIME).replace(tzinfo=UTC)
    return moment.astimezone(zone(tz)).isoformat()


def end_of_local_day(day: date, tz: str | None) -> datetime:
    """23:59:59 local on `day`: a date-only deadline stays on that date in the caller's zone."""
    return datetime.combine(day, time(23, 59, 59), tzinfo=zone(tz))


def to_html(text: str | None) -> str | None:
    """Plain text → the HTML Odoo stores in Html fields; HTML passes through.

    Odoo sanitises Html fields itself. Plain text would otherwise lose its line
    breaks, and a literal `<` in it would be read as markup.
    """
    if text is None:
        return None
    if _HTML_TAG.search(text):
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def m2o(value: Any) -> dict[str, Any] | None:
    """`[id, name]` (search_read) or `{id, display_name}` (web_read) → `{id, name}`."""
    if isinstance(value, list | tuple) and len(value) == 2:
        return {"id": value[0], "name": value[1]}
    if isinstance(value, dict) and value.get("id"):
        return {"id": value["id"], "name": value.get("display_name")}
    return None


def x2m(value: Any) -> list[dict[str, Any]]:
    """web_read x2many `[{id, display_name}, …]` → `[{id, name}, …]`; bare ids pass as `{id}`."""
    out: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, dict):
            out.append({"id": item.get("id"), "name": item.get("display_name")})
        else:
            out.append({"id": item, "name": None})
    return out


def int_ids(values: list[Any] | None, label: str) -> list[int]:
    """Validate a list of record ids: integers, positive, de-duplicated, order kept."""
    ids: list[int] = []
    for v in values or []:
        try:
            i = int(v)
        except (TypeError, ValueError) as e:
            raise ValidationError(f"{label} must be a list of integer ids, got {v!r}") from e
        if i <= 0:
            raise ValidationError(f"{label} must contain positive ids, got {v!r}")
        if i not in ids:
            ids.append(i)
    return ids
