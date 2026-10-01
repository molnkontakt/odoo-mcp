"""To-dos (Odoo's To-do app, module project_todo): list, create, change, tick off.

A to-do in Odoo is a `project.task` **without a project** (and without a
parent task). Tasks in projects are a different thing — they have stages,
customers, timesheets and followers who expect to hear about changes — so
every tool here refuses a task that has a project or a parent. The generic
readers can see `project.task`; nothing but these tools writes it.

**Quiet by default.** Assigning someone a task makes Odoo mail them "You have
been assigned to …", and state changes are tracked into the chatter where a
follower may be subscribed to them. Every write here carries:

- ``mail_auto_subscribe_no_notify`` — no assignment mail
  (``project.task._task_message_auto_subscribe_notify``),
- ``mail_create_nolog`` — no "task created" log entry,
- ``mail_notrack`` — no tracking messages (state, assignees, deadline).

Assignees still become followers, as in Odoo's UI; following sends nothing
by itself.

**Assignees.** Like the To-do app, a new to-do is assigned to the caller when
`user_ids` is not given, and Odoo always adds the caller to a new to-do's
assignees — a private task the creator cannot see would be lost. On update,
`user_ids` replaces the list as given.

**Tags** are matched against existing `project.tags` by name (case-insensitive)
and never created: an assistant inventing tag names would litter the tag list,
so an unknown name is an error that lists it.
"""

from __future__ import annotations

import xmlrpc.client
from typing import Any

from odoo_mcp.app import mcp
from odoo_mcp.audit import audit_call
from odoo_mcp.auth import SCOPE_READ, SCOPE_WRITE, requires_scope
from odoo_mcp.client import get_client
from odoo_mcp.instances import Instance, resolve_instance
from odoo_mcp.tools._common import (
    DEFAULT_TZ,
    end_of_local_day,
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

MODEL = "project.task"

STATE_OPEN = "01_in_progress"
STATE_DONE = "1_done"
#: Odoo's CLOSED_STATES for project.task (Odoo 17+).
CLOSED_STATES: tuple[str, ...] = ("1_done", "1_canceled")

#: Context for every write; see the module docstring.
QUIET_CONTEXT: dict[str, Any] = {
    "mail_auto_subscribe_no_notify": True,
    "mail_create_nolog": True,
    "mail_notrack": True,
}

PRIORITIES: tuple[str, ...] = ("0", "1", "2", "3")
STATUSES: tuple[str, ...] = ("open", "done", "all")

#: Same ordering as the To-do app's kanban.
_ORDER = "priority desc, date_deadline asc, id desc"

_SPEC: dict[str, Any] = {
    "name": {},
    "state": {},
    "priority": {},
    "date_deadline": {},
    "active": {},
    "user_ids": {"fields": {"display_name": {}}},
    "tag_ids": {"fields": {"display_name": {}}},
}

_MISSING_MARKERS = ("doesn't exist", "does not exist", "invalid model or method")


def _require_todo_app(client: Any, instance: str) -> None:
    """The To-do app adds `get_todo_views_id` to project.task; call it as the probe.

    That separates "project_todo missing" from "project missing" (no model at
    all) without reading `ir.module.module`, which only administrators may.
    """
    try:
        client.execute_kw(MODEL, "get_todo_views_id", [])
    except xmlrpc.client.Fault as fault:
        if any(marker in str(fault.faultString) for marker in _MISSING_MARKERS):
            raise ValidationError(
                f"The To-do app (module project_todo) is not installed on {instance}; "
                f"the to-do tools are unavailable there."
            ) from None
        raise


def _load_todo(client: Any, todo_id: int, instance: str) -> dict[str, Any]:
    """Read a task and refuse anything that is not a to-do."""
    rows = client.execute_kw(
        MODEL, "read", [[int(todo_id)]],
        {"fields": ["id", "name", "state", "project_id", "parent_id"], "context": {"active_test": False}},
    )
    if not rows:
        raise ValidationError(f"To-do {todo_id} not found on {instance}")
    row = dict(rows[0])
    project = m2o(row.get("project_id"))
    if project:
        raise ValidationError(
            f"Task {todo_id} belongs to the project {project['name']!r}. These tools only handle "
            f"to-dos (tasks without a project); project tasks are out of scope."
        )
    parent = m2o(row.get("parent_id"))
    if parent:
        raise ValidationError(
            f"Task {todo_id} is a sub-task of {parent['name']!r}. These tools only handle "
            f"top-level to-dos (no project, no parent task)."
        )
    return row


def require_todo(client: Any, todo_id: int, instance: str) -> None:
    """For other tools (attachment upload): is `todo_id` a to-do this server may write?"""
    _require_todo_app(client, instance)
    _load_todo(client, todo_id, instance)


def _caller_uid(client: Any) -> int:
    """The Odoo user the call runs as — with act-as-caller, the human, not the service account.

    project.task's default_get links the current user to a new to-do
    (``Command.link(env.user.id)``), so asking for that default answers
    "who am I" through any route, gateway included, with no ACL on res.users.
    """
    defaults = client.execute_kw(MODEL, "default_get", [["user_ids"]]) or {}
    for command in defaults.get("user_ids") or []:
        if isinstance(command, list | tuple) and command:
            if command[0] == 4 and len(command) >= 2:
                return int(command[1])
            if command[0] == 6 and len(command) >= 3 and len(command[2]) == 1:
                return int(command[2][0])
    raise ValidationError("Could not tell which Odoo user this call runs as; use mine=False.")


def _resolve_tags(client: Any, names: list[str], instance: str) -> list[int]:
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return []
    domain: list[Any] = ["|"] * (len(wanted) - 1) + [("name", "=ilike", n) for n in wanted]
    rows = client.execute_kw("project.tags", "search_read", [domain], {"fields": ["id", "name"]})
    by_name: dict[str, int] = {}
    for r in rows or []:
        by_name.setdefault(str(r["name"]).casefold(), int(r["id"]))
    missing = [n for n in wanted if n.casefold() not in by_name]
    if missing:
        raise ValidationError(
            f"Unknown tag(s) on {instance}: {', '.join(missing)}. This tool only uses existing "
            f"tags; create the tag in Odoo first, or leave it out."
        )
    ids: list[int] = []
    for n in wanted:
        tag_id = by_name[n.casefold()]
        if tag_id not in ids:
            ids.append(tag_id)
    return ids


def _priority(value: int | str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text not in PRIORITIES:
        raise ValidationError(f"priority must be 0 (normal) to 3 (urgent); got {value!r}")
    return text


def _deadline(value: str, tz: str | None) -> str | bool:
    """'' clears; a date means the end of that day in `tz`; a datetime is taken as given."""
    text = str(value).strip()
    if not text:
        return False
    if len(text) == 10:
        return to_odoo_utc(end_of_local_day(parse_date(text, "deadline"), tz))
    return to_odoo_utc(parse_local_datetime(text, tz, "deadline"))


def _todo_result(row: dict[str, Any], tz: str | None) -> dict[str, Any]:
    deadline_at = from_odoo_utc(row.get("date_deadline"), tz)
    state = row.get("state")
    return {
        "todo_id": row.get("id"),
        "name": row.get("name"),
        "state": state,
        "done": state == STATE_DONE,
        "deadline": deadline_at[:10] if deadline_at else None,
        "deadline_at": deadline_at,
        "priority": row.get("priority"),
        "tags": [t["name"] for t in x2m(row.get("tag_ids"))],
        "assignees": x2m(row.get("user_ids")),
        "active": row.get("active", True),
    }


def _read_todo(client: Any, todo_id: int, tz: str | None) -> dict[str, Any]:
    rows = client.execute_kw(
        MODEL, "web_read", [[int(todo_id)]], {"specification": _SPEC, "context": {"active_test": False}},
    )
    return _todo_result(dict(rows[0]) if rows else {"id": int(todo_id)}, tz)


def _write_values(
    client: Any,
    instance: str,
    tz: str | None,
    name: str | None,
    description: str | None,
    deadline: str | None,
    user_ids: list[int] | None,
    tags: list[str] | None,
    priority: int | str | None,
) -> dict[str, Any]:
    vals: dict[str, Any] = {}
    if name is not None:
        if not name.strip():
            raise ValidationError("name must not be empty")
        vals["name"] = name.strip()
    if description is not None:
        vals["description"] = to_html(description) or False
    if deadline is not None:
        vals["date_deadline"] = _deadline(deadline, tz)
    if user_ids is not None:
        vals["user_ids"] = [(6, 0, int_ids(user_ids, "user_ids"))]
    if tags is not None:
        vals["tag_ids"] = [(6, 0, _resolve_tags(client, tags, instance))]
    prio = _priority(priority)
    if prio is not None:
        vals["priority"] = prio
    return vals


@mcp.tool()
@requires_scope(SCOPE_READ)
def odoo_list_todos(
    mine: bool = True,
    status: str = "open",
    query: str | None = None,
    limit: int = 50,
    timezone: str = DEFAULT_TZ,
    instance: Instance | None = None,
) -> list[dict[str, Any]]:
    """To-dos from Odoo's To-do app: tasks without a project.

    Ordered like the app: highest priority first, then nearest deadline.

    Args:
        mine: only to-dos assigned to the caller (default True); False lists
            every to-do the caller may see (Odoo's record rules decide)
        status: "open" (default), "done" or "all" ("open" includes anything
            not done or cancelled)
        query: case-insensitive text in the title or description
        limit: max rows (default 50)
        timezone: zone the deadlines are shown in (default Europe/Stockholm)
        instance: instance name; may be omitted when only one is configured

    Returns:
        [{todo_id, name, state, done, deadline, deadline_at, priority, tags,
          assignees: [{id, name}], active}]
    """
    if status not in STATUSES:
        raise ValidationError(f"status must be one of {list(STATUSES)}, got {status!r}")
    zone(timezone)
    instance = resolve_instance(instance)
    client = get_client(instance)
    _require_todo_app(client, instance)
    domain: list[Any] = [("project_id", "=", False), ("parent_id", "=", False)]
    if mine:
        domain.append(("user_ids", "in", [_caller_uid(client)]))
    if status == "open":
        domain.append(("state", "not in", list(CLOSED_STATES)))
    elif status == "done":
        domain.append(("state", "=", STATE_DONE))
    if query:
        domain += ["|", ("name", "ilike", query), ("description", "ilike", query)]
    res = client.execute_kw(
        MODEL, "web_search_read", [domain],
        {"specification": _SPEC, "limit": max(1, int(limit)), "order": _ORDER},
    ) or {}
    return [_todo_result(dict(r), timezone) for r in res.get("records", [])]


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_create_todo(
    name: str,
    description: str | None = None,
    deadline: str | None = None,
    user_ids: list[int] | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    timezone: str = DEFAULT_TZ,
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Create a to-do (a task without a project) — nobody is mailed.

    Without `user_ids` the to-do is assigned to the caller, as in the To-do
    app. With `user_ids`, Odoo still adds the caller, so the creator keeps
    seeing it. Assignees are not sent Odoo's "you have been assigned" mail.

    Args:
        name: the to-do's title
        description: text (plain text keeps its line breaks) or HTML
        deadline: "YYYY-MM-DD" (end of that day in `timezone`) or a date and time
        user_ids: res.users ids to assign (default: the caller)
        tags: names of existing tags (project.tags), matched case-insensitively;
            unknown names are refused, never created
        priority: 0 normal (default) … 3 urgent; the To-do app shows 1 as a star
        timezone: IANA zone for `deadline` (default Europe/Stockholm)
        instance: instance name; may be omitted when only one is configured

    Returns:
        {todo_id, name, state, done, deadline, deadline_at, priority, tags,
         assignees, active}
    """
    if not (name or "").strip():
        raise ValidationError("name must not be empty")
    zone(timezone)
    instance = resolve_instance(instance)
    client = get_client(instance)
    _require_todo_app(client, instance)
    audit_params = {
        "name": name, "deadline": deadline, "user_ids": user_ids, "tags": tags, "priority": priority,
        "description_chars": len(description or ""),
    }
    with audit_call(tool="odoo_create_todo", instance=instance, params=audit_params) as ctx:
        vals = _write_values(client, instance, timezone, name, description, deadline, user_ids, tags, priority)
        todo_id = int(client.execute_kw(MODEL, "create", [vals], {"context": dict(QUIET_CONTEXT)}))
        ctx.mark_committed()
        result = _read_todo(client, todo_id, timezone)
        ctx.summary = f"created to-do id={todo_id} assignees={[a['id'] for a in result['assignees']]}"
        return result


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_update_todo(
    todo_id: int,
    name: str | None = None,
    description: str | None = None,
    deadline: str | None = None,
    user_ids: list[int] | None = None,
    tags: list[str] | None = None,
    priority: int | None = None,
    timezone: str = DEFAULT_TZ,
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Change a to-do. Only the fields given change; nobody is mailed.

    `user_ids` and `tags` replace the current lists. An empty string clears
    `description` or `deadline`. Removing yourself from `user_ids` can make the
    to-do invisible to you (Odoo shows a private task only to its assignees).
    Refused for a task that has a project or a parent task.

    Args:
        todo_id: project.task id (`odoo_list_todos`)
        name, description, deadline, user_ids, tags, priority, timezone:
            as for `odoo_create_todo`
        instance: instance name; may be omitted when only one is configured

    Returns:
        the to-do as in `odoo_create_todo`, plus `updated` (field names)
    """
    zone(timezone)
    instance = resolve_instance(instance)
    client = get_client(instance)
    _require_todo_app(client, instance)
    audit_params = {
        "todo_id": int(todo_id), "name": name, "deadline": deadline, "user_ids": user_ids,
        "tags": tags, "priority": priority,
        "description_chars": None if description is None else len(description),
    }
    with audit_call(tool="odoo_update_todo", instance=instance, params=audit_params) as ctx:
        _load_todo(client, int(todo_id), instance)
        vals = _write_values(client, instance, timezone, name, description, deadline, user_ids, tags, priority)
        if not vals:
            raise ValidationError("Nothing to update: give at least one field to change.")
        client.execute_kw(MODEL, "write", [[int(todo_id)], vals], {"context": dict(QUIET_CONTEXT)})
        ctx.mark_committed()
        result = _read_todo(client, int(todo_id), timezone)
        result["updated"] = sorted(vals)
        ctx.summary = f"to-do {todo_id} updated {','.join(sorted(vals))}"
        return result


@mcp.tool()
@requires_scope(SCOPE_WRITE)
def odoo_set_todo_state(
    todo_id: int,
    done: bool,
    instance: Instance | None = None,
) -> dict[str, Any]:
    """Tick a to-do off (`done=True`) or open it again (`done=False`). Nobody is mailed.

    Odoo 17+ keeps this in `project.task.state`: done is "1_done", open is
    "01_in_progress" (what Odoo's own checkmark sets). Re-opening a cancelled
    to-do also sets it in progress. Refused for a task that has a project.

    Args:
        todo_id: project.task id (`odoo_list_todos`)
        done: True to mark done, False to re-open
        instance: instance name; may be omitted when only one is configured

    Returns:
        {todo_id, name, previous_state, state, done, changed}
    """
    instance = resolve_instance(instance)
    client = get_client(instance)
    _require_todo_app(client, instance)
    target = STATE_DONE if done else STATE_OPEN
    with audit_call(
        tool="odoo_set_todo_state", instance=instance, params={"todo_id": int(todo_id), "done": bool(done)},
    ) as ctx:
        row = _load_todo(client, int(todo_id), instance)
        previous = row.get("state")
        changed = previous != target and not (not done and previous not in CLOSED_STATES)
        if changed:
            client.execute_kw(MODEL, "write", [[int(todo_id)], {"state": target}], {"context": dict(QUIET_CONTEXT)})
            ctx.mark_committed()
        rows = client.execute_kw(MODEL, "read", [[int(todo_id)]], {"fields": ["state"]})
        state = rows[0].get("state") if rows else target
        ctx.summary = f"to-do {todo_id} state {previous} -> {state}"
        return {
            "todo_id": int(todo_id),
            "name": row.get("name"),
            "previous_state": previous,
            "state": state,
            "done": state == STATE_DONE,
            "changed": changed,
        }
