"""Calendar (calendar.event): create, change and archive events — quietly by default.

An assistant that puts a meeting in the calendar should not, as a side effect,
mail every attendee an invitation, and moving a meeting by an hour should not
send a "date changed" notice to people who were never told about it. Odoo does
both on its own. So every tool here takes `notify` (default **false**); while
it is false the write carries the context keys that make Odoo's calendar keep
quiet, and no reminders (alarms) are set:

- ``no_mail_to_attendees`` — stops invitation and change-of-date mails
  (``calendar.attendee._notify_attendees``),
- ``skip_attendee_notification`` — the same mails, one level up
  (``calendar.event.write``),
- ``dont_notify`` — no alarm triggers or bus notifications,
- ``mail_create_nolog`` — no "event created" chatter entry.

With ``notify=true`` none of these are sent and Odoo behaves as in its own UI.

**Synchronised events are not ours to change.** An event that a calendar
synchronisation module maintains carries that module's key, and a change made
here would be overwritten by the next sync — silently, from the user's point
of view. When the database has the field ``l10n_se_cc_key`` and it is set on an
event, update and archive refuse. The field is looked up with ``fields_get``
on every call, so the tools work the same on a database without that module.

**Archive, never delete.** `odoo_archive_calendar_event` sets
``active=False``; the event stays restorable from Odoo's archive filter.
"""

from __future__ import annotations

import xmlrpc.client
from datetime import datetime
from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call
from odoo_mcp.auth import SCOPE_WRITE, requires_scope
from odoo_mcp.client import get_client, model_fields
from odoo_mcp.instances import Instance, resolve_instance
from odoo_mcp.tools._common import (
    DEFAULT_TZ,
    ODOO_DATETIME,
    from_odoo_utc,
    int_ids,
    m2o,
    parse_date,
    parse_local_datetime,
    to_html,
    to_odoo_utc,
    x2m,
    zone,
)
from odoo_mcp.validators import ValidationError

MODEL = "calendar.event"

#: Set by a calendar synchronisation module on the events it owns.
SYNC_KEY_FIELD = "l10n_se_cc_key"

#: Context for `notify=False`. See the module docstring for what each key stops.
SILENT_CONTEXT: dict[str, Any] = {
    "no_mail_to_attendees": True,
    "skip_attendee_notification": True,
    "dont_notify": True,
    "mail_create_nolog": True,
}

RECURRENCE_UPDATES: tuple[str, ...] = ("self_only", "future_events", "all_events")

#: Odoo's own convention for all-day events: start/stop hold 08:00 and 18:00
#: on the day (not UTC) — see calendar.event._inverse_dates.
ALLDAY_START = "08:00:00"
ALLDAY_STOP = "18:00:00"

_SPEC: dict[str, Any] = {
    "name": {},
    "allday": {},
    "start": {},
    "stop": {},
    "start_date": {},
    "stop_date": {},
    "location": {},
    "active": {},
    "recurrency": {},
    "user_id": {"fields": {"display_name": {}}},
    "partner_ids": {"fields": {"display_name": {}}},
}


def _calendar_has_sync_key(client: Any, instance: str) -> bool:
    """Probe the model once per call: missing app → clear error; else is the sync key there?"""
    fields = model_fields(client, MODEL, ["id", SYNC_KEY_FIELD])
    if fields is None:
        raise ValidationError(
            f"The Calendar app (module calendar) is not installed on {instance}; "
            f"the calendar tools are unavailable there."
        )
    return SYNC_KEY_FIELD in fields


def _context(notify: bool) -> dict[str, Any]:
    return {} if notify else dict(SILENT_CONTEXT)


def _check_recurrence_update(value: str) -> str:
    if value not in RECURRENCE_UPDATES:
        raise ValidationError(f"recurrence_update must be one of {list(RECURRENCE_UPDATES)}, got {value!r}")
    return value


def _load_event(client: Any, event_id: int, has_sync: bool, instance: str) -> dict[str, Any]:
    fields = ["id", "name", "allday", "start", "stop", "active", "recurrence_id"]
    if has_sync:
        fields.append(SYNC_KEY_FIELD)
    rows = client.execute_kw(MODEL, "read", [[int(event_id)]], {"fields": fields})
    if not rows:
        raise ValidationError(f"Calendar event {event_id} not found on {instance}")
    return dict(rows[0])


def _refuse_synced(
    client: Any, event: dict[str, Any], has_sync: bool, recurrence_update: str, action: str
) -> None:
    """Refuse to touch an event (or, for a series change, any event of its series) a sync owns."""
    if not has_sync:
        return
    message = (
        f"Calendar event {event['id']} is maintained by a calendar synchronisation "
        f"({SYNC_KEY_FIELD} is set). A change made here would be overwritten by the next "
        f"sync, so this tool will not {action} it — change it where it is synchronised from."
    )
    if event.get(SYNC_KEY_FIELD):
        raise ValidationError(message)
    recurrence = m2o(event.get("recurrence_id"))
    if recurrence and recurrence_update != "self_only":
        synced = client.execute_kw(
            MODEL, "search_count",
            [[("recurrence_id", "=", recurrence["id"]), (SYNC_KEY_FIELD, "!=", False)]],
            {"context": {"active_test": False}},
        )
        if synced:
            raise ValidationError(
                message.replace(f"Calendar event {event['id']} is", f"The series of event {event['id']} has events")
            )


def _time_values(
    date: str | None,
    end_date: str | None,
    start: str | None,
    stop: str | None,
    tz: str | None,
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Odoo values for the event's time, from whichever form the caller used."""
    zone(tz)  # fail early on a bad zone name, whichever form is used
    if date and (start or stop):
        raise ValidationError("Give either date/end_date (all-day event) or start/stop (timed event), not both.")
    if end_date and not date:
        raise ValidationError("end_date needs date (all-day event); for a timed event use start/stop.")
    if date:
        first = parse_date(date, "date")
        last = parse_date(end_date, "end_date") if end_date else first
        if last < first:
            raise ValidationError(f"end_date {last} is before date {first}")
        return {
            "allday": True,
            "start": f"{first.isoformat()} {ALLDAY_START}",
            "stop": f"{last.isoformat()} {ALLDAY_STOP}",
            "start_date": first.isoformat(),
            "stop_date": last.isoformat(),
        }
    if not start and not stop:
        return {}
    if stop and not start:
        if current is None:
            raise ValidationError("start is required with stop")
        return {"stop": to_odoo_utc(parse_local_datetime(stop, tz, "stop"))}
    assert start is not None
    begin = parse_local_datetime(start, tz, "start")
    if stop:
        end = parse_local_datetime(stop, tz, "stop")
    elif current is not None and not current.get("allday") and current.get("start") and current.get("stop"):
        # Moving a timed event: keep its length.
        length = datetime.strptime(str(current["stop"]), ODOO_DATETIME) - datetime.strptime(
            str(current["start"]), ODOO_DATETIME
        )
        end = begin + length
    else:
        raise ValidationError("stop is required with start (e.g. start='2026-10-05 10:00', stop='2026-10-05 11:00')")
    if end <= begin:
        raise ValidationError(f"stop ({stop or end.isoformat()}) must be after start ({start})")
    return {"allday": False, "start": to_odoo_utc(begin), "stop": to_odoo_utc(end)}


def _check_partners(client: Any, partner_ids: list[int], instance: str) -> None:
    if not partner_ids:
        return
    rows = client.execute_kw("res.partner", "read", [partner_ids], {"fields": ["id"]})
    found = {int(r["id"]) for r in rows or []}
    missing = [p for p in partner_ids if p not in found]
    if missing:
        raise ValidationError(f"Attendee partner id(s) not found on {instance}: {missing}")


def _read_event(client: Any, event_id: int) -> dict[str, Any]:
    rows = client.execute_kw(
        MODEL, "web_read", [[int(event_id)]],
        {"specification": _SPEC, "context": {"active_test": False}},
    )
    return dict(rows[0]) if rows else {"id": int(event_id)}


def _event_result(row: dict[str, Any], tz: str | None, notified: bool) -> dict[str, Any]:
    allday = bool(row.get("allday"))
    result: dict[str, Any] = {
        "event_id": row.get("id"),
        "name": row.get("name"),
        "allday": allday,
        "start": row.get("start_date") if allday else from_odoo_utc(row.get("start"), tz),
        "stop": row.get("stop_date") if allday else from_odoo_utc(row.get("stop"), tz),
        "timezone": None if allday else (tz or DEFAULT_TZ),
        "location": row.get("location") or None,
        "organizer": m2o(row.get("user_id")),
        "attendees": x2m(row.get("partner_ids")),
        "recurring": bool(row.get("recurrency")),
        "active": row.get("active", True),
        "notified": notified,
    }
    return result


def _summary_line(result: dict[str, Any]) -> str:
    when = result["start"] if result["allday"] else f"{result['start']} – {result['stop']}"
    return f"{result['name']} ({when}), {len(result['attendees'])} attendee(s)"


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_calendar_event(
    name: str,
    date: str | None = None,
    end_date: str | None = None,
    start: str | None = None,
    stop: str | None = None,
    timezone: str = DEFAULT_TZ,
    description: str | None = None,
    location: str | None = None,
    attendee_partner_ids: list[int] | None = None,
    notify: bool = False,
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Create a calendar event — without mailing anyone unless `notify=True`.

    Either an all-day event (`date`, optional `end_date`) or a timed one
    (`start` and `stop`). Times without an offset are wall-clock time in
    `timezone` (default Europe/Stockholm, daylight saving handled); Odoo stores
    UTC and the conversion happens here. `2026-10-05T10:00:00+02:00` is taken
    as given.

    Attendees: without `attendee_partner_ids` Odoo makes the caller (the
    logged-in user's contact) the only attendee, as in its own UI. With
    `attendee_partner_ids` the list is exactly those contacts — include the
    caller's own partner id to be on it too.

    `notify=False` (default): no invitations, no reminders, no chatter log —
    the event is simply in the calendar. `notify=True`: Odoo sends its usual
    invitations to attendees (for future events).

    Args:
        name: the event title
        date: all-day event, first day YYYY-MM-DD
        end_date: all-day event, last day YYYY-MM-DD (default: same as date)
        start: timed event start, e.g. "2026-10-05 18:00"
        stop: timed event end, e.g. "2026-10-05 19:30"
        timezone: IANA zone for start/stop without offset (default Europe/Stockholm)
        description: text (plain text keeps its line breaks) or HTML
        location: free text, e.g. a room or an address
        attendee_partner_ids: res.partner ids (`odoo_search_partners`)
        notify: send Odoo's invitation mails (default False)
        instance: instance name; may be omitted when only one is configured

    Returns:
        {event_id, name, allday, start, stop, timezone, location, organizer,
         attendees: [{id, name}], recurring, active, notified, summary}
    """
    if not (name or "").strip():
        raise ValidationError("name must not be empty")
    if not date and not start:
        raise ValidationError("Give date (all-day event) or start and stop (timed event).")
    times = _time_values(date, end_date, start, stop, timezone)
    partners = int_ids(attendee_partner_ids, "attendee_partner_ids") if attendee_partner_ids is not None else None

    instance = resolve_instance(instance)
    client = get_client(instance)
    _calendar_has_sync_key(client, instance)
    audit_params = {
        "name": name, "date": date, "end_date": end_date, "start": start, "stop": stop,
        "timezone": timezone, "location": location, "attendee_partner_ids": partners, "notify": bool(notify),
    }
    with audit_call(tool="odoo_create_calendar_event", instance=instance, params=audit_params) as ctx:
        if partners:
            _check_partners(client, partners, instance)
        vals: dict[str, Any] = {"name": name.strip(), **times}
        if description is not None:
            vals["description"] = to_html(description)
        if location:
            vals["location"] = location
        if partners is not None:
            vals["partner_ids"] = [(6, 0, partners)]
        if not notify:
            vals["alarm_ids"] = [(6, 0, [])]
        event_id = int(client.execute_kw(MODEL, "create", [vals], {"context": _context(notify)}))
        ctx.mark_committed()
        result = _event_result(_read_event(client, event_id), timezone, bool(notify))
        result["summary"] = _summary_line(result)
        ctx.summary = f"created calendar event id={event_id} notify={bool(notify)}"
        return result


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_update_calendar_event(
    event_id: int,
    name: str | None = None,
    date: str | None = None,
    end_date: str | None = None,
    start: str | None = None,
    stop: str | None = None,
    timezone: str = DEFAULT_TZ,
    description: str | None = None,
    location: str | None = None,
    attendee_partner_ids: list[int] | None = None,
    notify: bool = False,
    recurrence_update: str = "self_only",
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Change a calendar event; a new time does not mail attendees unless `notify=True`.

    Only the fields given change. `date`/`end_date` make it an all-day event,
    `start`/`stop` a timed one; `start` alone moves a timed event and keeps its
    length. `attendee_partner_ids` replaces the attendee list. An empty string
    for `description` or `location` clears it.

    Refused for an event a calendar synchronisation maintains (see the module
    notes): the change would be overwritten by the next sync.

    Args:
        event_id: calendar.event id
        name, date, end_date, start, stop, timezone, description, location,
            attendee_partner_ids: as for `odoo_create_calendar_event`
        notify: let Odoo send its invitation / date-changed mails (default False)
        recurrence_update: for an event in a recurring series — "self_only"
            (default; this occurrence only), "future_events" or "all_events"
        instance: instance name; may be omitted when only one is configured

    Returns:
        the event as in `odoo_create_calendar_event`, plus `updated` (field names)
    """
    _check_recurrence_update(recurrence_update)
    partners = int_ids(attendee_partner_ids, "attendee_partner_ids") if attendee_partner_ids is not None else None
    if name is not None and not name.strip():
        raise ValidationError("name must not be empty")

    instance = resolve_instance(instance)
    client = get_client(instance)
    has_sync = _calendar_has_sync_key(client, instance)
    audit_params = {
        "event_id": int(event_id), "name": name, "date": date, "end_date": end_date, "start": start,
        "stop": stop, "timezone": timezone, "location": location, "attendee_partner_ids": partners,
        "notify": bool(notify), "recurrence_update": recurrence_update,
    }
    with audit_call(tool="odoo_update_calendar_event", instance=instance, params=audit_params) as ctx:
        event = _load_event(client, int(event_id), has_sync, instance)
        _refuse_synced(client, event, has_sync, recurrence_update, "change")

        vals: dict[str, Any] = _time_values(date, end_date, start, stop, timezone, current=event)
        if name is not None:
            vals["name"] = name.strip()
        if description is not None:
            vals["description"] = to_html(description) or False
        if location is not None:
            vals["location"] = location or False
        if partners is not None:
            _check_partners(client, partners, instance)
            vals["partner_ids"] = [(6, 0, partners)]
        if not vals:
            raise ValidationError("Nothing to update: give at least one field to change.")
        updated = sorted(vals)
        if m2o(event.get("recurrence_id")):
            vals["recurrence_update"] = recurrence_update

        client.execute_kw(MODEL, "write", [[int(event_id)], vals], {"context": _context(notify)})
        ctx.mark_committed()
        result = _event_result(_read_event(client, int(event_id)), timezone, bool(notify))
        result["updated"] = updated
        result["summary"] = _summary_line(result)
        ctx.summary = f"calendar event {event_id} updated {','.join(updated)} notify={bool(notify)}"
        return result


def _is_none_marshalling_fault(fault: xmlrpc.client.Fault) -> bool:
    """Odoo's XML-RPC cannot return None: a method that returns nothing comes back
    as a fault *after* its transaction committed. action_mass_archive is one."""
    return "cannot marshal None" in str(fault.faultString)


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_archive_calendar_event(
    event_id: int,
    recurrence_update: str = "self_only",
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Archive a calendar event (active=False). Nothing is deleted, and nobody is mailed.

    The event disappears from calendars and stays restorable from Odoo's
    *Archived* filter. For an event in a recurring series,
    `recurrence_update` says how much of the series goes: "self_only"
    (default), "future_events" (this one and the ones after it) or
    "all_events". Refused for an event a calendar synchronisation maintains.

    Args:
        event_id: calendar.event id
        recurrence_update: "self_only" (default), "future_events" or "all_events"
        instance: instance name; may be omitted when only one is configured

    Returns:
        {event_id, name, archived, recurrence_update, archived_count}
    """
    _check_recurrence_update(recurrence_update)
    instance = resolve_instance(instance)
    client = get_client(instance)
    has_sync = _calendar_has_sync_key(client, instance)
    with audit_call(
        tool="odoo_archive_calendar_event", instance=instance,
        params={"event_id": int(event_id), "recurrence_update": recurrence_update},
    ) as ctx:
        event = _load_event(client, int(event_id), has_sync, instance)
        _refuse_synced(client, event, has_sync, recurrence_update, "archive")
        recurrence = m2o(event.get("recurrence_id"))
        silent = {"context": dict(SILENT_CONTEXT)}

        if recurrence:
            series_domain: list[Any] = [("recurrence_id", "=", recurrence["id"])]
            before = int(client.execute_kw(MODEL, "search_count", [series_domain]) or 0)
            try:
                # Odoo's own entry point for archiving (part of) a series: it
                # trims the recurrence rule for "future_events" as the UI does.
                client.execute_kw(MODEL, "action_mass_archive", [[int(event_id)], recurrence_update], silent)
            except xmlrpc.client.Fault as fault:
                if not _is_none_marshalling_fault(fault):
                    raise
            after = int(client.execute_kw(MODEL, "search_count", [series_domain]) or 0)
            archived_count = max(before - after, 0)
        else:
            client.execute_kw(MODEL, "write", [[int(event_id)], {"active": False}], silent)
            archived_count = 1
        ctx.mark_committed()

        rows = client.execute_kw(
            MODEL, "read", [[int(event_id)]], {"fields": ["active"], "context": {"active_test": False}},
        )
        archived = bool(rows) and not rows[0].get("active", True)
        ctx.summary = f"calendar event {event_id} archived={archived} ({recurrence_update}, {archived_count})"
        return {
            "event_id": int(event_id),
            "name": event.get("name"),
            "archived": archived,
            "recurrence_update": recurrence_update if recurrence else None,
            "archived_count": archived_count,
        }
