"""To-do tools: only tasks without a project, never a mail to assignees."""

from __future__ import annotations

import xmlrpc.client

import pytest

from odoo_mcp import client as client_module
from odoo_mcp.tools import todos
from odoo_mcp.validators import ValidationError

QUIET_KEYS = {"mail_auto_subscribe_no_notify", "mail_create_nolog", "mail_notrack"}

TODO_ROW = {
    "id": 40, "name": "Order new keys", "state": "01_in_progress", "priority": "1",
    "date_deadline": "2026-10-09 21:59:59", "active": True,
    "user_ids": [{"id": 2, "display_name": "Alex Example"}, {"id": 6, "display_name": "Sam Example"}],
    "tag_ids": [{"id": 3, "display_name": "Board"}],
}


@pytest.fixture
def patched_client(mock_client, monkeypatch):
    monkeypatch.setattr(client_module, "get_client", lambda inst: mock_client)
    monkeypatch.setattr(todos, "get_client", lambda inst: mock_client)
    return mock_client


def _state(task: dict | None = None, **overrides):
    state = {
        "project.task": {
            "get_todo_views_id": [[1, "kanban"]],
            "default_get": {"user_ids": [[4, 2]]},
            "read": [{"id": 40, "name": "Order new keys", "state": "01_in_progress",
                      "project_id": False, "parent_id": False, **(task or {})}],
            "web_read": [TODO_ROW],
            "web_search_read": {"length": 1, "records": [TODO_ROW]},
            "create": 40,
            "write": True,
        },
        "project.tags": {"search_read": [{"id": 3, "name": "Board"}, {"id": 4, "name": "Garden"}]},
    }
    state.update(overrides)
    return state


def _call(client, method, model="project.task"):
    return next(c for c in client.calls if c[0] == model and c[1] == method)


class TestList:
    def test_mine_open_uses_callers_uid_and_todo_domain(self, patched_client):
        patched_client.state = _state()
        rows = todos.odoo_list_todos(query="keys", instance="dev")
        _, _, args, kwargs = _call(patched_client, "web_search_read")
        domain = args[0]
        assert ("project_id", "=", False) in domain and ("parent_id", "=", False) in domain
        assert ("user_ids", "in", [2]) in domain, "uid from default_get, not the service account"
        assert ("state", "not in", ["1_done", "1_canceled"]) in domain
        assert "|" in domain and ("name", "ilike", "keys") in domain
        assert kwargs["limit"] == 50
        assert rows == [{
            "todo_id": 40, "name": "Order new keys", "state": "01_in_progress", "done": False,
            "deadline": "2026-10-09", "deadline_at": "2026-10-09T23:59:59+02:00", "priority": "1",
            "tags": ["Board"], "assignees": [{"id": 2, "name": "Alex Example"}, {"id": 6, "name": "Sam Example"}],
            "active": True,
        }]

    def test_all_visible_done(self, patched_client):
        patched_client.state = _state()
        todos.odoo_list_todos(mine=False, status="done", instance="dev")
        domain = _call(patched_client, "web_search_read")[2][0]
        assert not [d for d in domain if d[0] == "user_ids"]
        assert ("state", "=", "1_done") in domain
        assert not [c for c in patched_client.calls if c[1] == "default_get"]

    def test_bad_status(self, patched_client):
        with pytest.raises(ValidationError, match="status"):
            todos.odoo_list_todos(status="later", instance="dev")

    def test_uid_from_set_command(self, patched_client):
        patched_client.state = _state()
        patched_client.state["project.task"]["default_get"] = {"user_ids": [[6, 0, [8]]]}
        todos.odoo_list_todos(instance="dev")
        assert ("user_ids", "in", [8]) in _call(patched_client, "web_search_read")[2][0]


class TestCreate:
    def test_quiet_context_and_values(self, patched_client):
        patched_client.state = _state()
        result = todos.odoo_create_todo(
            name=" Order new keys ", description="Two keys\nfor the shed", deadline="2026-10-09",
            user_ids=[6], tags=["board"], priority=1, instance="dev",
        )
        _, _, args, kwargs = _call(patched_client, "create")
        vals = args[0]
        assert kwargs["context"] == {k: True for k in QUIET_KEYS}
        assert vals["name"] == "Order new keys"
        assert vals["description"] == "<p>Two keys<br>for the shed</p>"
        assert vals["date_deadline"] == "2026-10-09 21:59:59", "end of the day in Stockholm, in UTC"
        assert vals["user_ids"] == [(6, 0, [6])]
        assert vals["tag_ids"] == [(6, 0, [3])]
        assert vals["priority"] == "1"
        assert "project_id" not in vals and "state" not in vals
        assert result["todo_id"] == 40

    def test_without_user_ids_odoo_assigns_the_caller(self, patched_client):
        patched_client.state = _state()
        todos.odoo_create_todo(name="x", instance="dev")
        assert "user_ids" not in _call(patched_client, "create")[2][0]

    def test_deadline_with_time(self, patched_client):
        patched_client.state = _state()
        todos.odoo_create_todo(name="x", deadline="2026-12-01 09:30", instance="dev")
        assert _call(patched_client, "create")[2][0]["date_deadline"] == "2026-12-01 08:30:00"

    def test_unknown_tags_are_refused_not_created(self, patched_client):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match="Unknown tag.*Secret"):
            todos.odoo_create_todo(name="x", tags=["Board", "Secret"], instance="dev")
        assert not [c for c in patched_client.calls if c[0] == "project.tags" and c[1] == "create"]
        assert not [c for c in patched_client.calls if c[1] == "create"]

    def test_tag_match_is_exact_not_a_wildcard(self, patched_client):
        # =ilike treats % and _ as wildcards; the tool compares names itself.
        patched_client.state = _state(**{"project.tags": {"search_read": [{"id": 3, "name": "Board"}]}})
        with pytest.raises(ValidationError, match="Unknown tag"):
            todos.odoo_create_todo(name="x", tags=["B%"], instance="dev")

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"name": " "}, "name"),
            ({"name": "x", "priority": 7}, "priority"),
            ({"name": "x", "deadline": "next week"}, "deadline"),
            ({"name": "x", "user_ids": ["me"]}, "user_ids"),
            ({"name": "x", "timezone": "Nowhere/Land"}, "time zone"),
        ],
    )
    def test_bad_input(self, patched_client, kwargs, match):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match=match):
            todos.odoo_create_todo(**kwargs, instance="dev")
        assert not [c for c in patched_client.calls if c[1] == "create"]


class TestProjectGuard:
    @pytest.mark.parametrize("task", [
        {"project_id": [5, "Renovation"]},
        {"parent_id": [39, "Parent to-do"]},
    ])
    @pytest.mark.parametrize("call", [
        lambda: todos.odoo_update_todo(todo_id=40, name="x", instance="dev"),
        lambda: todos.odoo_set_todo_state(todo_id=40, done=True, instance="dev"),
    ])
    def test_project_and_sub_tasks_are_refused(self, patched_client, task, call):
        patched_client.state = _state(task=task)
        with pytest.raises(ValidationError, match="only handle"):
            call()
        assert not [c for c in patched_client.calls if c[1] == "write"]

    def test_missing_task(self, patched_client):
        patched_client.state = _state()
        patched_client.state["project.task"]["read"] = []
        with pytest.raises(ValidationError, match="To-do 41 not found"):
            todos.odoo_update_todo(todo_id=41, name="x", instance="dev")


class TestUpdate:
    def test_quiet_write_of_given_fields_only(self, patched_client):
        patched_client.state = _state()
        result = todos.odoo_update_todo(todo_id=40, deadline="", user_ids=[2, 6], tags=[], instance="dev")
        _, _, args, kwargs = _call(patched_client, "write")
        assert args == [[40], {"date_deadline": False, "user_ids": [(6, 0, [2, 6])], "tag_ids": [(6, 0, [])]}]
        assert kwargs["context"] == {k: True for k in QUIET_KEYS}, "assigning must not mail the assignee"
        assert result["updated"] == ["date_deadline", "tag_ids", "user_ids"]

    def test_nothing_to_update(self, patched_client):
        patched_client.state = _state()
        with pytest.raises(ValidationError, match="Nothing to update"):
            todos.odoo_update_todo(todo_id=40, instance="dev")


class TestSetState:
    def test_done(self, patched_client):
        patched_client.state = _state()
        reads = iter([[{"id": 40, "name": "k", "state": "01_in_progress", "project_id": False, "parent_id": False}],
                      [{"id": 40, "state": "1_done"}]])
        patched_client.state["project.task"]["read"] = lambda a, k: next(reads)
        result = todos.odoo_set_todo_state(todo_id=40, done=True, instance="dev")
        _, _, args, kwargs = _call(patched_client, "write")
        assert args == [[40], {"state": "1_done"}]
        assert kwargs["context"] == {k: True for k in QUIET_KEYS}
        assert result == {"todo_id": 40, "name": "k", "previous_state": "01_in_progress", "state": "1_done",
                          "done": True, "changed": True}

    def test_reopen(self, patched_client):
        patched_client.state = _state(task={"state": "1_done"})
        todos.odoo_set_todo_state(todo_id=40, done=False, instance="dev")
        assert _call(patched_client, "write")[2] == [[40], {"state": "01_in_progress"}]

    def test_reopen_open_task_is_a_no_op(self, patched_client):
        patched_client.state = _state(task={"state": "04_waiting_normal"})
        result = todos.odoo_set_todo_state(todo_id=40, done=False, instance="dev")
        assert result["changed"] is False
        assert not [c for c in patched_client.calls if c[1] == "write"]


class TestModuleMissing:
    @pytest.mark.parametrize("fault", [
        "Object project.task doesn't exist",
        "The method 'project.task.get_todo_views_id' does not exist",
        "odoo.exceptions.AccessError: mcp.gateway: invalid model or method",
    ])
    @pytest.mark.parametrize("call", [
        lambda: todos.odoo_list_todos(instance="dev"),
        lambda: todos.odoo_create_todo(name="x", instance="dev"),
        lambda: todos.odoo_update_todo(todo_id=40, name="x", instance="dev"),
        lambda: todos.odoo_set_todo_state(todo_id=40, done=True, instance="dev"),
    ])
    def test_project_todo_not_installed(self, patched_client, fault, call):
        def missing(args, kwargs):
            raise xmlrpc.client.Fault(2, fault)

        patched_client.state = {"project.task": {"get_todo_views_id": missing}}
        with pytest.raises(ValidationError, match=r"module project_todo\) is not installed"):
            call()
        assert [c[1] for c in patched_client.calls] == ["get_todo_views_id"]

    def test_other_faults_surface(self, patched_client):
        def denied(args, kwargs):
            raise xmlrpc.client.Fault(3, "Access Denied")

        patched_client.state = {"project.task": {"get_todo_views_id": denied}}
        with pytest.raises(xmlrpc.client.Fault, match="Access Denied"):
            todos.odoo_list_todos(instance="dev")
