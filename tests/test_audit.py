"""Tests for the audit logger.

The old suite only checked the no-op path with the env var unset, which meant
the parts that actually protect the ledger — replay lookup, unique-violation
handling, reconnect behaviour — were never executed. These use a fake psycopg2
so those paths run for real.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from odoo_mcp import audit


@pytest.fixture(autouse=True)
def _reset_logger_singleton(monkeypatch):
    monkeypatch.setattr(audit, "_logger_instance", None)


# --------------------------------------------------------------------------
# Fake psycopg2
# --------------------------------------------------------------------------


class UniqueViolation(Exception):
    pgcode = "23505"


class FakeCursor:
    def __init__(self, conn: FakeConn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, args: tuple[Any, ...] = ()):
        normalized = " ".join(sql.split())
        self.conn.executed.append((normalized, args))
        self._last_was_insert = normalized.upper().startswith("INSERT")
        self._last_was_regclass = "to_regclass" in normalized
        if self.conn.raise_on_execute:
            exc = self.conn.raise_on_execute
            self.conn.raise_on_execute = None
            raise exc

    def fetchone(self):
        # The connect-time existence probe. Controlled separately so the
        # missing-table path can be tested too.
        if getattr(self, "_last_was_regclass", False):
            return (self.conn.table_exists and "mcp_audit" or None,)
        if self.conn.fetch_result is not None:
            return self.conn.fetch_result
        # INSERT ... RETURNING id — hand back a row id so the two-phase
        # completion path actually runs.
        if getattr(self, "_last_was_insert", False):
            self.conn.next_id += 1
            return (self.conn.next_id,)
        return None


class FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_result: Any = None
        self.raise_on_execute: Exception | None = None
        self.autocommit = False
        self.closed = False
        self.next_id = 0
        self.table_exists = True

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_pg(monkeypatch):
    """Install a fake psycopg2 and return a handle to control it."""
    state = types.SimpleNamespace(conns=[], connect_error=None, connect_calls=0)

    def connect(url):
        state.connect_calls += 1
        if state.connect_error:
            raise state.connect_error
        conn = FakeConn()
        state.conns.append(conn)
        return conn

    module = types.ModuleType("psycopg2")
    module.connect = connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setenv("MCP_AUDIT_DB_URL", "postgresql://fake/db")
    return state


# --------------------------------------------------------------------------


class TestConfiguration:
    def test_not_configured_without_env(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        log = audit.get_logger()
        assert log.configured is False
        with pytest.raises(audit.AuditUnavailable):
            log.ensure_available()

    def test_claim_is_none_when_best_effort_and_unconfigured(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        log = audit.get_logger()
        assert log.claim(tool="t", instance="dev", params={}, best_effort=True) is None

    def test_claim_raises_when_critical_and_unconfigured(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        log = audit.get_logger()
        with pytest.raises(audit.AuditUnavailable):
            log.claim(tool="t", instance="dev", params={}, best_effort=False)


class TestSchemaProvisioning:
    """The server verifies the audit table; it does not create it.

    The hardened role has no CREATE on the schema, because a process that can
    create its own audit table can also replace it — and the table is the
    evidence about that very process.
    """

    def test_connect_probes_for_the_table_instead_of_creating_it(self, fake_pg):
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        statements = [s[0] for s in fake_pg.conns[-1].executed]
        assert any("to_regclass" in s for s in statements)
        assert not any(s.upper().startswith("CREATE TABLE") for s in statements)

    def test_missing_table_is_a_clear_failure(self, fake_pg):
        log = audit.get_logger()

        original_connect = sys.modules["psycopg2"].connect

        def connect_with_missing_table(url):
            conn = original_connect(url)
            conn.table_exists = False
            return conn

        sys.modules["psycopg2"].connect = connect_with_missing_table
        with pytest.raises(audit.AuditUnavailable):
            log.claim(tool="t", instance="dev", params={}, best_effort=False)

    def test_auto_ddl_opt_in_creates_the_table(self, fake_pg, monkeypatch):
        monkeypatch.setenv("MCP_AUDIT_AUTO_DDL", "1")
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        statements = [s[0] for s in fake_pg.conns[-1].executed]
        assert any("CREATE TABLE IF NOT EXISTS mcp_audit" in s for s in statements)


class TestNoPermanentLatch:
    """A Postgres blip must not disable auditing for the process lifetime."""

    def test_recovers_after_a_failed_connect(self, fake_pg, monkeypatch):
        fake_pg.connect_error = OSError("connection refused")
        log = audit.get_logger()

        with pytest.raises(audit.AuditUnavailable):
            log.claim(tool="t", instance="dev", params={}, best_effort=False)

        # Still configured — the old code latched _enabled to False here and
        # never tried again.
        assert log.configured is True

        # Backoff is in effect, so an immediate retry is refused without even
        # reconnecting…
        calls_before = fake_pg.connect_calls
        with pytest.raises(audit.AuditUnavailable):
            log.claim(tool="t", instance="dev", params={}, best_effort=False)
        assert fake_pg.connect_calls == calls_before

        # …but once the backoff expires and Postgres is healthy again, it works.
        fake_pg.connect_error = None
        monkeypatch.setattr(log, "_retry_after", 0.0)
        row_id = log.claim(tool="t", instance="dev", params={}, best_effort=False)
        assert isinstance(row_id, int)
        assert fake_pg.connect_calls > calls_before

    def test_dead_connection_is_discarded(self, fake_pg):
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        conn = fake_pg.conns[-1]
        assert log._conn is conn

        conn.raise_on_execute = RuntimeError("server closed the connection")
        with pytest.raises(audit.AuditUnavailable):
            log.claim(tool="t", instance="dev", params={}, best_effort=False)

        # The old code kept the unusable connection, so every later call failed.
        assert log._conn is None
        assert conn.closed is True


class TestUniqueViolation:
    def test_unique_violation_propagates(self, fake_pg):
        """Swallowing this is what let a duplicate run while erasing the proof."""
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        fake_pg.conns[-1].raise_on_execute = UniqueViolation("duplicate key")

        with pytest.raises(UniqueViolation):
            log.claim(
                tool="t", instance="dev", params={},
                idempotency_key="k1", best_effort=True,
            )
        # Not treated as a broken connection — the database is fine, the
        # caller is wrong.
        assert log._conn is not None


class TestFindSuccessful:
    def test_returns_row_and_status(self, fake_pg):
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        fake_pg.conns[-1].fetch_result = (
            5, "2026-08-13", "odoo_post_journal_entry", "posted move 100",
            {"move_id": 100}, "ok", "abc123",
        )
        row = log.find_successful(
            idempotency_key="k1", instance="dev", tool="odoo_post_journal_entry"
        )
        assert row is not None
        assert row["status"] == "ok"
        assert row["params_fingerprint"] == "abc123"

    def test_query_includes_blocking_statuses(self, fake_pg):
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        conn = fake_pg.conns[-1]
        conn.fetch_result = None
        log.find_successful(idempotency_key="k1", instance="dev", tool="t")
        sql, args = conn.executed[-1]
        # in_progress and unknown must block a replay: both mean a write may
        # already have landed.
        assert "status = ANY" in sql
        assert set(args[3]) >= {"committed", "ok", "unknown", "in_progress"}

    def test_raises_instead_of_returning_none_on_db_error(self, fake_pg):
        """Fail closed: an unanswerable question is not 'no prior call'."""
        log = audit.get_logger()
        log.claim(tool="t", instance="dev", params={}, best_effort=True)
        fake_pg.conns[-1].raise_on_execute = RuntimeError("connection reset")
        with pytest.raises(audit.AuditUnavailable):
            log.find_successful(idempotency_key="k1", instance="dev", tool="t")

    def test_raises_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        with pytest.raises(audit.AuditUnavailable):
            audit.get_logger().find_successful(
                idempotency_key="k1", instance="dev", tool="t"
            )


class TestFailureClassification:
    def test_committed_wins_over_everything(self):
        assert audit.classify_failure(ValueError("parse"), committed=True) == "committed"

    def test_transport_errors_are_unknown(self):
        # socket.timeout is an alias of TimeoutError on modern Python.
        assert audit.classify_failure(TimeoutError(), committed=False) == "unknown"
        assert (
            audit.classify_failure(ConnectionResetError(), committed=False) == "unknown"
        )

    def test_application_errors_are_clean_failures(self):
        import xmlrpc.client

        # A Fault means Odoo answered and rejected — nothing was applied.
        fault = xmlrpc.client.Fault(4, "access denied")
        assert audit.classify_failure(fault, committed=False) == "error"
        assert audit.classify_failure(ValueError("bad"), committed=False) == "error"


class TestAuditCall:
    def test_marks_ok_on_success(self, fake_pg):
        with audit.audit_call(tool="t", instance="dev", params={}) as ctx:
            ctx.summary = "done"
        conn = fake_pg.conns[-1]
        update = [s for s in conn.executed if s[0].startswith("UPDATE")]
        assert update and update[-1][1][0] == "ok"

    def test_marks_committed_when_body_fails_after_mark(self, fake_pg):
        with pytest.raises(ValueError), audit.audit_call(
            tool="t", instance="dev", params={}
        ) as ctx:
            ctx.mark_committed()
            raise ValueError("parsing blew up after Odoo committed")
        conn = fake_pg.conns[-1]
        update = [s for s in conn.executed if s[0].startswith("UPDATE")]
        assert update and update[-1][1][0] == "committed"

    def test_propagates_body_exception(self, fake_pg):
        with pytest.raises(RuntimeError, match="boom"), audit.audit_call(
            tool="t", instance="dev", params={}
        ):
            raise RuntimeError("boom")

    def test_critical_refuses_when_unavailable(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        monkeypatch.delenv("MCP_ALLOW_UNAUDITED_CRITICAL_WRITES", raising=False)
        with pytest.raises(audit.AuditUnavailable), audit.audit_call(
            tool="t", instance="dev", params={}, critical=True
        ):
            pytest.fail("body must not run")

    def test_critical_opt_out_allows_unaudited(self, monkeypatch):
        monkeypatch.delenv("MCP_AUDIT_DB_URL", raising=False)
        monkeypatch.setenv("MCP_ALLOW_UNAUDITED_CRITICAL_WRITES", "1")
        ran = []
        with audit.audit_call(tool="t", instance="dev", params={}, critical=True):
            ran.append(True)
        assert ran == [True]


class TestFingerprint:
    def test_stable_regardless_of_key_order(self):
        a = audit.fingerprint_params({"b": 2, "a": 1})
        b = audit.fingerprint_params({"a": 1, "b": 2})
        assert a == b

    def test_differs_on_different_values(self):
        assert audit.fingerprint_params({"move_id": 1}) != audit.fingerprint_params(
            {"move_id": 2}
        )
