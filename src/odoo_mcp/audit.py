"""Audit log for MCP tool calls.

When `MCP_AUDIT_DB_URL` is set in the environment, every tool call is
recorded into a `mcp_audit` table. When the env var is absent the audit
logger becomes a no-op so the server still works for local development
without any external dependencies.

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
    duration_ms     INTEGER
);
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
                             response_summary, error, duration_ms)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                        """,
                        (
                            session_id,
                            instance,
                            tool,
                            json.dumps(params or {}, default=str),
                            response_summary,
                            error,
                            duration_ms,
                        ),
                    )
            except Exception:
                logger.exception("Audit log insert failed; row dropped.")


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
):
    """Context manager that times a tool call and writes an audit row.

    Usage:
        with audit_call(tool="odoo_post", instance="prod", params={...}):
            ... do work ...

    On exception the row records the exception text in `error`. On success
    the caller can set a response summary by setting `ctx.summary`:

        with audit_call(tool="...", ...) as ctx:
            ctx.summary = f"created move id={move_id}"
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
            )
        except Exception:
            logger.exception("Failed to write audit record")
