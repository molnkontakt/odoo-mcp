"""Calendar tools: quiet by default, UTC on the wire, synced events left alone."""

from __future__ import annotations

import xmlrpc.client

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.tools import calendar_events as cal
from odoo_mcp.validators import ValidationError

SILENT_KEYS = {"no_mail_to_attendees", "skip_attendee_notification", "dont_notify", "mail_create_nolog"}

EVENT_ROW = {
    "id": 77, "name": "Board meeting", "allday": False, "start": "2026-10-05 16:00:00",
    "stop": "2026-10-05 17:30:00", "start_date": False, "stop_date": False, "location": "Club house",
    "active": True, "recurrency": False, "user_id": {"id": 2, "display_name": "Alex Example"},
    "partner_ids": [{"id": 3, "display_name": "Alex Example"}],
}


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(cal, "get_client", lambda inst: mock_client)
    return mock_client


def _state(*, sync_field: bool = False, event: dict | None = None, **overrides):
    fields = {"id": {"type": "integer"}}
    if sync_field:
        fields[cal.SYNC_KEY_FIELD] = {"type": "char"}
    base = {
        "id": 77, "name": "Board meeting", "allday": False, "start": "2026-10-05 16:00:00",
        "stop": "2026-10-05 17:30:00", "active": True, "recurrence_id": False,
    }
    if sync_field:
        base[cal.SYNC_KEY_FIELD] = False
    state = {
        "calendar.event": {
            "fields_get": fields,
            "create": 77,
            "write": True,
            "read": [{**base, **(event or {})}],
            "web_read": [EVENT_ROW],
            "search_count": 0,
        },
        "res.partner": {"read": lambda args, kw: [{"id": i} for i in args[0]]},
    }
    state.update(overrides)
    return state


def _call(client, method, model="calendar.event"):
    return next(c for c in client.calls if c[0] == model and c[1] == method)


class TestCreate:
    def test_timed_event_is_converted_to_utc_and_silent(self, patched_client):
        patched_client.state = _state()
        result = cal.odoo_create_calendar_event(
            name="Board meeting", start="2026-10-05 18:00", stop="2026-10-05 19:30",
            location="Club house", description="Agenda\n\n1. Budget", instance="dev",
        )
        _, _, args, kwargs = _call(patched_client, "create")
        vals = args[0]
        # Europe/Stockholm is UTC+2 in early October (summer time).
        assert vals["start"] == "2026-10-05 16:00:00" and vals["stop"] == "2026-10-05 17:30:00"
        assert vals["allday"] is False
        assert vals["alarm_ids"] == [(6, 0, [])], "no reminders when notify is off"
        assert vals["description"] == "<p>Agenda</p><p>1. Budget</p>"
        assert "partner_ids" not in vals, "Odoo's default (the caller) is kept"
        assert set(kwargs["context"]) == SILENT_KEYS and all(kwargs["context"].values())
        assert result["event_id"] == 77 and result["notified"] is False
        assert result["start"] == "2026-10-05T18:00:00+02:00", "shown back in local time"
        assert result["attendees"] == [{"id": 3, "name": "Alex Example"}]
        assert "Board meeting" in result["summary"]

    def test_winter_time_offset(self, patched_client):
        patched_client.state = _state()
        cal.odoo_create_calendar_event(name="x", start="2026-12-01 09:00", stop="2026-12-01 10:00", instance="dev")
        vals = _call(patched_client, "create")[2][0]
        assert vals["start"] == "2026-12-01 08:00:00", "UTC+1 in winter"

    def test_explicit_offset_and_other_zone(self, patched_client):
        patched_client.state = _state()
        cal.odoo_create_calendar_event(
            name="x", start="2026-10-05T10:00:00Z", stop="2026-10-05 13:00", timezone="America/New_York",
            instance="dev",
        )
        vals = _call(patched_client, "create")[2][0]
        assert vals["start"] == "2026-10-05 10:00:00", "an explicit offset wins over timezone"
        assert vals["stop"] == "2026-10-05 17:00:00", "naive stop read in America/New_York (UTC-4)"

    def test_dst_change_night(self, patched_client):
        patched_client.state = _state()
        # Summer time ends 2026-10-25 03:00 local -> 02:00.
        cal.odoo_create_calendar_event(name="x", start="2026-10-24 22:00", stop="2026-10-25 06:00", instance="dev")
        vals = _call(patched_client, "create")[2][0]
        assert vals["start"] == "2026-10-24 20:00:00"
        assert vals["stop"] == "2026-10-25 05:00:00"

    def test_allday_uses_odoo_convention(self, patched_client):
        patched_client.state = _state()
        cal.odoo_create_calendar_event(name="Camp", date="2026-10-10", end_date="2026-10-11", instance="dev")
        vals = _call(patched_client, "create")[2][0]
        assert vals["allday"] is True
        assert vals["start_date"] == "2026-10-10" and vals["stop_date"] == "2026-10-11"
        assert vals["start"] == "2026-10-10 08:00:00" and vals["stop"] == "2026-10-11 18:00:00"

    def test_attendees_replace_the_default_and_are_checked(self, patched_client):
        patched_client.state = _state()
        cal.odoo_create_calendar_event(name="x", date="2026-10-10", attendee_partner_ids=[5, 3, 5], instance="dev")
        vals = _call(patched_client, "create")[2][0]
        assert vals["partner_ids"] == [(6, 0, [5, 3])]
        patched_client.state = _state(**{"res.partner": {"read": [{"id": 3}]}})
        with pytest.raises(ValidationError, match=r"not found.*\[5\]"):
            cal.odoo_create_calendar_event(name="x", date="2026-10-10", attendee_partner_ids=[3, 5], instance="dev")

    def test_notify_true_sends_no_silencing_context_and_no_alarm_override(self, patched_client):
        patched_client.state = _state()
        result = cal.odoo_create_calendar_event(
            name="x", start="2026-10-05 18:00", stop="2026-10-05 19:00", notify=True, instance="dev",
        )
        _, _, args, kwargs = _call(patched_client, "create")
        assert kwargs["context"] == {}
        assert "alarm_ids" not in args[0]
        assert result["notified"] is True

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"name": " ", "date": "2026-10-10"}, "name"),
            ({"name": "x"}, "date"),
            ({"name": "x", "date": "2026-10-10", "start": "2026-10-10 10:00"}, "not both"),
            ({"name": "x", "date": "2026-10-10", "end_date": "2026-10-09"}, "before"),
            ({"name": "x", "start": "2026-10-10 10:00"}, "stop is required"),
            ({"name": "x", "start": "2026-10-10 10:00", "stop": "2026-10-10 09:00"}, "after start"),
            ({"name": "x", "start": "2026-10-10", "stop": "2026-10-10 11:00"}, "time of day"),
            ({"name": "x", "start": "tomorrow", "stop": "2026-10-10 11:00"}, "date and time"),
            ({"name": "x", "date": "10/10/2026"}, "YYYY-MM-DD"),
            ({"name": "x", "date": "2026-10-10", "timezone": "Mars/Olympus"}, "time zone"),
            ({"name": "x", "end_date": "2026-10-10", "start": "2026-10-10 10:00"}, "end_date needs date"),
        ],
    )
    def test_bad_input_never_reaches_odoo(self, patched_client, kwargs, match):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match=match):
            cal.odoo_create_calendar_event(**kwargs, instance="dev")
        assert patched_client.calls == []


class TestUpdate:
    def test_moving_keeps_length_and_stays_silent(self, patched_client):
        patched_client.state = _state()
        result = cal.odoo_update_calendar_event(event_id=77, start="2026-10-06 18:00", instance="dev")
        _, _, args, kwargs = _call(patched_client, "write")
        assert args[0] == [77]
        assert args[1] == {"allday": False, "start": "2026-10-06 16:00:00", "stop": "2026-10-06 17:30:00"}
        assert set(kwargs["context"]) == SILENT_KEYS, "a new time must not mail attendees"
        assert result["updated"] == ["allday", "start", "stop"]
        assert "recurrence_update" not in args[1], "only sent for events in a series"

    def test_notify_true_lets_odoo_mail(self, patched_client):
        patched_client.state = _state()
        cal.odoo_update_calendar_event(event_id=77, start="2026-10-06 18:00", notify=True, instance="dev")
        assert _call(patched_client, "write")[3]["context"] == {}

    def test_clearing_and_replacing_fields(self, patched_client):
        patched_client.state = _state()
        cal.odoo_update_calendar_event(
            event_id=77, name=" New name ", location="", description="", attendee_partner_ids=[9], instance="dev",
        )
        vals = _call(patched_client, "write")[2][1]
        assert vals == {"name": "New name", "location": False, "description": False, "partner_ids": [(6, 0, [9])]}

    def test_recurring_event_passes_recurrence_update(self, patched_client):
        patched_client.state = _state(event={"recurrence_id": [4, "Weekly"]})
        cal.odoo_update_calendar_event(event_id=77, name="x", instance="dev")
        assert _call(patched_client, "write")[2][1]["recurrence_update"] == "self_only"

    def test_nothing_to_update(self, patched_client):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match="Nothing to update"):
            cal.odoo_update_calendar_event(event_id=77, instance="dev")
        assert not [c for c in patched_client.calls if c[1] == "write"]

    def test_unknown_event(self, patched_client):
        patched_client.state = _state()
        patched_client.state["calendar.event"]["read"] = []
        with pytest.raises(ValidationError, match="Calendar event 99 not found"):
            cal.odoo_update_calendar_event(event_id=99, name="x", instance="dev")

    def test_bad_recurrence_update(self, patched_client):
        with pytest.raises(ValidationError, match="recurrence_update"):
            cal.odoo_update_calendar_event(event_id=77, name="x", recurrence_update="everything", instance="dev")


class TestSyncGuard:
    def test_synced_event_is_refused_for_update_and_archive(self, patched_client):
        patched_client.state = _state(sync_field=True, event={cal.SYNC_KEY_FIELD: "abc-123"})
        with pytest.raises(ValidationError, match="synchronisation.*overwritten"):
            cal.odoo_update_calendar_event(event_id=77, name="x", instance="dev")
        with pytest.raises(ValidationError, match="synchronisation"):
            cal.odoo_archive_calendar_event(event_id=77, instance="dev")
        assert not [c for c in patched_client.calls if c[1] in ("write", "action_mass_archive")]
        read = _call(patched_client, "read")
        assert cal.SYNC_KEY_FIELD in read[3]["fields"]

    def test_unsynced_event_passes_when_field_exists(self, patched_client):
        patched_client.state = _state(sync_field=True)
        cal.odoo_update_calendar_event(event_id=77, name="x", instance="dev")
        assert _call(patched_client, "write")

    def test_without_the_field_it_is_not_requested(self, patched_client):
        patched_client.state = _state(sync_field=False)
        cal.odoo_update_calendar_event(event_id=77, name="x", instance="dev")
        read = _call(patched_client, "read")
        assert cal.SYNC_KEY_FIELD not in read[3]["fields"], "asking for a missing field would fault"
        probe = _call(patched_client, "fields_get")
        assert cal.SYNC_KEY_FIELD in probe[2][0]

    def test_series_change_checks_the_whole_series(self, patched_client):
        patched_client.state = _state(sync_field=True, event={"recurrence_id": [4, "Weekly"]})
        patched_client.state["calendar.event"]["search_count"] = 2
        with pytest.raises(ValidationError, match="series"):
            cal.odoo_archive_calendar_event(event_id=77, recurrence_update="all_events", instance="dev")
        # self_only only looks at the event itself
        cal.odoo_archive_calendar_event(event_id=77, recurrence_update="self_only", instance="dev")


class TestArchive:
    def test_single_event_is_archived_not_deleted(self, patched_client):
        patched_client.state = _state()
        reads = iter([[{"id": 77, "name": "Board meeting", "recurrence_id": False, "active": True}],
                      [{"id": 77, "active": False}]])
        patched_client.state["calendar.event"]["read"] = lambda a, k: next(reads)
        result = cal.odoo_archive_calendar_event(event_id=77, instance="dev")
        _, _, args, kwargs = _call(patched_client, "write")
        assert args == [[77], {"active": False}]
        assert set(kwargs["context"]) == SILENT_KEYS
        assert not [c for c in patched_client.calls if c[1] == "unlink"]
        assert result == {"event_id": 77, "name": "Board meeting", "archived": True,
                          "recurrence_update": None, "archived_count": 1}

    def test_series_goes_through_odoos_mass_archive(self, patched_client):
        patched_client.state = _state(event={"recurrence_id": [4, "Weekly"]})
        counts = iter([5, 2])
        patched_client.state["calendar.event"]["search_count"] = lambda a, k: next(counts)

        def none_result(args, kwargs):
            raise xmlrpc.client.Fault(1, "TypeError: cannot marshal None unless allow_none is enabled")

        patched_client.state["calendar.event"]["action_mass_archive"] = none_result
        result = cal.odoo_archive_calendar_event(event_id=77, recurrence_update="future_events", instance="dev")
        _, _, args, kwargs = _call(patched_client, "action_mass_archive")
        assert args == [[77], "future_events"]
        assert set(kwargs["context"]) == SILENT_KEYS
        assert result["archived_count"] == 3 and result["recurrence_update"] == "future_events"

    def test_other_faults_from_mass_archive_surface(self, patched_client):
        patched_client.state = _state(event={"recurrence_id": [4, "Weekly"]})

        def denied(args, kwargs):
            raise xmlrpc.client.Fault(3, "AccessError")

        patched_client.state["calendar.event"]["action_mass_archive"] = denied
        with pytest.raises(xmlrpc.client.Fault, match="AccessError"):
            cal.odoo_archive_calendar_event(event_id=77, recurrence_update="all_events", instance="dev")


class TestModuleMissing:
    @staticmethod
    def _missing(args, kwargs):
        raise xmlrpc.client.Fault(2, "Object calendar.event doesn't exist")

    @pytest.mark.parametrize("call", [
        lambda: cal.odoo_create_calendar_event(name="x", date="2026-10-10", instance="dev"),
        lambda: cal.odoo_update_calendar_event(event_id=1, name="x", instance="dev"),
        lambda: cal.odoo_archive_calendar_event(event_id=1, instance="dev"),
    ])
    def test_calendar_not_installed(self, patched_client, call):
        patched_client.state = {"calendar.event": {"fields_get": self._missing}}
        with pytest.raises(ValidationError, match=r"module calendar\) is not installed"):
            call()
        assert [c[1] for c in patched_client.calls] == ["fields_get"]
