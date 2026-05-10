"""Audit log for MCP tool calls.

When `MCP_AUDIT_DB_URL` is set in the environment, write_safe tool calls
are recorded into a `mcp_audit` table. When the env var is absent the
audit logger becomes a silent no-op so the server still works for local
development without any external dependencies.

Read tools are not audit-logged today (they don't change state). Phase 3
will extend coverage to write_critical tools.

The table schema (auto-created on first connect) matches the design in
docs/ARCHITECTURE.md:

    CREATE TABLE mcp_audit (
        id              SERIAL PRIMARY KEY,
        ts              TIMESTAMPTZ DEFAULT now(),
        session_id      TEXT,
        instance        TEXT,
        tool            TEXT NOT NULL,
        params          JSONB,
        response_summary TEXT,
        error           TEXT,
        duration_ms     INTEGER
    );
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS mcp_audit (
    id              SERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT now(),
    session_id      TEXT,
    instance        TEXT,
    tool            TEXT NOT NULL,
    params          JSONB,
    response_summary TEXT,
    error           TEXT,
    duration_ms     INTEGER,
    idempotency_key TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS mcp_audit_idempotency_idx
    ON mcp_audit (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND error IS NULL;
"""


class AuditLogger:
    """Thread-safe singleton that owns the PostgreSQL connection.

    Lazily connects on first record() call so that import order doesn't
    matter and tests can monkeypatch the env var.
    """

    def __init__(self) -> None:
        self._conn: Any = None
        self._lock = threading.Lock()
        self._enabled = bool(os.environ.get("MCP_AUDIT_DB_URL"))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _connect(self) -> Any:
        url = os.environ.get("MCP_AUDIT_DB_URL")
        if not url:
            return None
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "psycopg2 not installed but MCP_AUDIT_DB_URL is set; "
                "audit logging disabled."
            )
            return None
        try:
            conn = psycopg2.connect(url)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_DDL)
            return conn
        except Exception:
            logger.exception("Failed to connect to audit database; logging disabled.")
            return None

    def record(
        self,
        *,
        tool: str,
        instance: str | None,
        params: dict[str, Any] | None,
        response_summary: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Append a single audit row. Silently no-ops if logging disabled."""
        if not self._enabled:
            return
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            if self._conn is None:
                self._enabled = False  # don't keep retrying
                return
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO mcp_audit
                            (session_id, instance, tool, params,
                             response_summary, error, duration_ms,
                             idempotency_key)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            instance,
                            tool,
                            json.dumps(params or {}, default=str),
                            response_summary,
                            error,
                            duration_ms,
                            idempotency_key,
                        ),
                    )
            except Exception:
                logger.exception("Audit log insert failed; row dropped.")

    def find_successful(
        self, *, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Return a previously-successful audit row matching this idempotency
        key, or None. Used by write_critical tools to short-circuit re-issued
        confirmed writes that already succeeded.
        """
        if not self._enabled or not idempotency_key:
            return None
        with self._lock:
            if self._conn is None:
                self._conn = self._connect()
            if self._conn is None:
                self._enabled = False
                return None
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, ts, tool, response_summary, params
                        FROM mcp_audit
                        WHERE idempotency_key = %s AND error IS NULL
                        ORDER BY ts DESC LIMIT 1
                        """,
                        (idempotency_key,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    return {
                        "id": row[0],
                        "ts": row[1],
                        "tool": row[2],
                        "response_summary": row[3],
                        "params": row[4],
                    }
            except Exception:
                logger.exception("Audit log idempotency lookup failed")
                return None


_logger_instance: AuditLogger | None = None


def get_logger() -> AuditLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = AuditLogger()
    return _logger_instance


@contextmanager
def audit_call(
    *,
    tool: str,
    instance: str | None,
    params: dict[str, Any] | None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
):
    """Context manager that times a tool call and writes an audit row.

    Usage:
        with audit_call(tool="odoo_post", instance="prod", params={...}):
            ... do work ...

    On exception the row records the exception text in `error`. On success
    the caller can set a response summary by setting `ctx.summary`:

        with audit_call(tool="...", ...) as ctx:
            ctx.summary = f"created move id={move_id}"

    Pass `idempotency_key` to enable replay-safety on write_critical tools:
    use `find_previous_success(idempotency_key)` before doing the work, and
    if that returns a row, return its summary instead of re-running.
    """
    class _Ctx:
        summary: str | None = None

    ctx = _Ctx()
    started = time.monotonic()
    err: str | None = None
    try:
        yield ctx
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        duration = int((time.monotonic() - started) * 1000)
        try:
            get_logger().record(
                tool=tool,
                instance=instance,
                params=params,
                response_summary=ctx.summary,
                error=err,
                duration_ms=duration,
                session_id=session_id,
                idempotency_key=idempotency_key,
            )
        except Exception:
            logger.exception("Failed to write audit record")


def find_previous_success(idempotency_key: str) -> dict[str, Any] | None:
    """Look up a successful prior call with this idempotency key.

    Tools should call this before performing critical writes; if the
    return value is non-None, return it as the result instead of doing
    the work again.
    """
    return get_logger().find_successful(idempotency_key=idempotency_key)
