"""Audit log for MCP tool calls.

This table is the only place that records *who* did *what* through this server.
Odoo's own history shows the shared service account for every action, so once a
service credential is used, this log is the sole attribution source — treat its
integrity as load-bearing, not as telemetry.

Two-phase writes
----------------
A row is **claimed** (`status='in_progress'`) *before* the Odoo call and
**completed** afterwards. That ordering is what makes idempotency real:

- The unique index on `(instance, tool, idempotency_key)` rejects a concurrent
  or repeated claim at the database, rather than in a check-then-act race
  between a SELECT and a much later INSERT.
- A process that dies mid-call leaves an `in_progress` row behind. That row is
  evidence a write may have landed, and it keeps the key burnt. The old
  write-once-in-`finally` design lost both.

Failure classification
----------------------
`error` means the call failed *before* Odoo could act. `committed` means Odoo
did the work (even if something afterwards, like result parsing, then blew up).
`unknown` means the request may have reached Odoo and we cannot tell — a socket
timeout or a dropped connection. Both `committed` and `unknown` block a replay,
because retrying either can double-apply a write.

Availability
------------
Critical writes fail **closed**: if this log cannot be reached, the tool refuses
rather than acting unrecorded. Set `MCP_ALLOW_UNAUDITED_CRITICAL_WRITES=1` to
opt out for local development only.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import socket
import threading
import time
import xmlrpc.client
from contextlib import contextmanager
from typing import Any

from odoo_mcp.auth import current_identity

logger = logging.getLogger(__name__)

#: How long to stop hammering a database that just refused us. Replaces the old
#: permanent latch, which turned one Postgres blip into an audit-less process
#: for the rest of its life — and these processes are long-lived.
RECONNECT_BACKOFF_SECONDS = 30.0

#: Statuses that mean "this may have hit Odoo" and therefore block a replay.
BLOCKING_STATUSES = ("committed", "ok", "unknown", "in_progress")

#: Exceptions that leave the outcome genuinely unknown: the request may already
#: have been written to the socket. `xmlrpc.client.Fault` is deliberately NOT
#: here — a Fault is Odoo answering, which means it processed and rejected the
#: call, so nothing was applied.
_UNKNOWN_OUTCOME_ERRORS: tuple[type[BaseException], ...] = (
    socket.timeout,
    ConnectionError,
    http.client.HTTPException,
    xmlrpc.client.ProtocolError,
)


class AuditUnavailable(RuntimeError):
    """Raised when a critical write cannot be audited, so must not proceed."""


_DDL = """
CREATE TABLE IF NOT EXISTS mcp_audit (
    id                 BIGSERIAL PRIMARY KEY,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_ts       TIMESTAMPTZ,
    status             TEXT NOT NULL DEFAULT 'in_progress',
    session_id         TEXT,
    instance           TEXT,
    ledger             TEXT,
    tool               TEXT NOT NULL,
    params             JSONB,
    params_fingerprint TEXT,
    response_summary   TEXT,
    error              TEXT,
    duration_ms        INTEGER,
    idempotency_key    TEXT,
    actor              TEXT,
    actor_sub          TEXT,
    client_id          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS mcp_audit_idempotency_idx
    ON mcp_audit (instance, tool, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


def fingerprint_params(params: dict[str, Any] | None) -> str:
    """Stable SHA-256 over the request parameters.

    Used to detect an idempotency key replayed with *different* arguments —
    without it, reusing a key on another move_id returns a cheerful
    `{"replayed": true}` for work that was never done.
    """
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditLogger:
    """Owns the PostgreSQL connection. Safe to share across threads."""

    def __init__(self) -> None:
        self._conn: Any = None
        self._lock = threading.Lock()
        self._retry_after = 0.0

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("MCP_AUDIT_DB_URL"))

    @property
    def enabled(self) -> bool:
        """Backwards-compatible alias for `configured`.

        Note this is deliberately *not* a health check — it says the log is
        meant to be on, not that it currently answers. Use `ensure_available()`
        before relying on it.
        """
        return self.configured

    def _drop_connection(self) -> None:
        """Discard a connection that just failed, and back off briefly.

        Resetting `_conn` matters: psycopg2 connections stay unusable after an
        error, so keeping the object meant every later call failed too.
        """
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
        self._conn = None
        self._retry_after = time.monotonic() + RECONNECT_BACKOFF_SECONDS

    def _get_conn(self) -> Any:
        """Return a live connection, or None. Caller must hold the lock."""
        if self._conn is not None:
            return self._conn
        if not self.configured:
            return None
        if time.monotonic() < self._retry_after:
            return None
        url = os.environ["MCP_AUDIT_DB_URL"]
        try:
            import psycopg2  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("psycopg2 not installed but MCP_AUDIT_DB_URL is set.")
            self._retry_after = time.monotonic() + RECONNECT_BACKOFF_SECONDS
            return None
        try:
            conn = psycopg2.connect(url)
            conn.autocommit = True
            with conn.cursor() as cur:
                if auto_ddl_enabled():
                    cur.execute(_DDL)
                else:
                    # Verify rather than create. The production role has no
                    # CREATE on the schema on purpose: a process that can
                    # create its own audit table can also replace it, and the
                    # table is the evidence. Provision it out of band.
                    cur.execute("SELECT to_regclass('public.mcp_audit')")
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        raise RuntimeError(
                            "Table mcp_audit does not exist and this role "
                            "cannot create it. Provision the schema first, or "
                            "set MCP_AUDIT_AUTO_DDL=1 for local development."
                        )
            self._conn = conn
            self._retry_after = 0.0
            return conn
        except Exception:
            logger.exception("Failed to connect to audit database.")
            self._drop_connection()
            return None

    def ensure_available(self) -> None:
        """Raise AuditUnavailable unless the log is configured and reachable."""
        if not self.configured:
            raise AuditUnavailable(
                "MCP_AUDIT_DB_URL is not set. Critical writes are refused "
                "because they would leave no record of who did what. Set the "
                "variable, or set MCP_ALLOW_UNAUDITED_CRITICAL_WRITES=1 for "
                "local development."
            )
        with self._lock:
            if self._get_conn() is None:
                raise AuditUnavailable(
                    "Audit database unreachable; refusing the write rather "
                    "than performing it unrecorded."
                )

    def claim(
        self,
        *,
        tool: str,
        instance: str | None,
        params: dict[str, Any] | None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        actor: str | None = None,
        actor_sub: str | None = None,
        client_id: str | None = None,
        params_fingerprint: str | None = None,
        best_effort: bool = False,
    ) -> int | None:
        """Insert an `in_progress` row and return its id.

        `params_fingerprint` should cover the *caller's arguments*, not the
        enriched context logged in `params`. The fingerprint answers "was this
        the same request?", so it must not shift because a re-read returned a
        different amount — and it must be computable without calling Odoo, so
        a replay check costs nothing.

        Raises psycopg2.errors.UniqueViolation if this idempotency key was
        already claimed — that propagates on purpose. Swallowing it is what
        let a duplicate run while deleting the evidence that it had.
        """
        if not self.configured:
            if best_effort:
                return None
            raise AuditUnavailable("MCP_AUDIT_DB_URL is not set.")
        with self._lock:
            conn = self._get_conn()
            if conn is None:
                if best_effort:
                    return None
                raise AuditUnavailable("Audit database unreachable.")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO mcp_audit
                            (session_id, instance, tool, params,
                             params_fingerprint, idempotency_key, actor,
                             actor_sub, client_id, status)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                                'in_progress')
                        RETURNING id
                        """,
                        (
                            session_id,
                            instance,
                            tool,
                            json.dumps(params or {}, default=str),
                            params_fingerprint or fingerprint_params(params),
                            idempotency_key,
                            actor,
                            actor_sub,
                            client_id,
                        ),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else None
            except Exception as exc:
                if _is_unique_violation(exc):
                    raise
                logger.exception("Audit claim failed.")
                self._drop_connection()
                if best_effort:
                    return None
                raise AuditUnavailable(f"Audit claim failed: {exc}") from exc

    def complete(
        self,
        row_id: int | None,
        *,
        status: str,
        response_summary: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Move a claimed row to a terminal status. Best-effort by design.

        The work is already done at this point; failing here must not turn a
        successful write into an exception. The `in_progress` row left behind
        is itself the signal that something needs reconciling.
        """
        if row_id is None or not self.configured:
            return
        with self._lock:
            conn = self._get_conn()
            if conn is None:
                return
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE mcp_audit
                           SET status = %s,
                               completed_ts = now(),
                               response_summary = %s,
                               error = %s,
                               duration_ms = %s
                         WHERE id = %s
                        """,
                        (status, response_summary, error, duration_ms, row_id),
                    )
            except Exception:
                logger.exception(
                    "Audit completion failed for row %s; it stays in_progress.", row_id
                )
                self._drop_connection()

    def find_successful(
        self,
        *,
        idempotency_key: str,
        instance: str,
        tool: str,
    ) -> dict[str, Any] | None:
        """Return a prior blocking row for this key, or None.

        Raises AuditUnavailable if the lookup cannot be performed. Returning
        None on a database error would silently degrade replay protection into
        "no protection" exactly when the database is misbehaving.
        """
        if not idempotency_key:
            return None
        if not self.configured:
            raise AuditUnavailable("MCP_AUDIT_DB_URL is not set.")
        with self._lock:
            conn = self._get_conn()
            if conn is None:
                raise AuditUnavailable("Audit database unreachable.")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id, ts, tool, response_summary, params,
                               status, params_fingerprint
                        FROM mcp_audit
                        WHERE idempotency_key = %s
                          AND instance = %s
                          AND tool = %s
                          AND status = ANY(%s)
                        ORDER BY ts DESC LIMIT 1
                        """,
                        (idempotency_key, instance, tool, list(BLOCKING_STATUSES)),
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
                        "status": row[5],
                        "params_fingerprint": row[6],
                    }
            except Exception as exc:
                logger.exception("Audit idempotency lookup failed.")
                self._drop_connection()
                raise AuditUnavailable(f"Idempotency lookup failed: {exc}") from exc


def _is_unique_violation(exc: BaseException) -> bool:
    return type(exc).__name__ == "UniqueViolation" or getattr(exc, "pgcode", None) == "23505"


def classify_failure(exc: BaseException, *, committed: bool) -> str:
    """Decide the terminal status for a failed call."""
    if committed:
        # Odoo already did the work; whatever broke happened afterwards.
        return "committed"
    if isinstance(exc, _UNKNOWN_OUTCOME_ERRORS):
        return "unknown"
    return "error"


_logger_instance: AuditLogger | None = None
_logger_lock = threading.Lock()


def get_logger() -> AuditLogger:
    global _logger_instance
    if _logger_instance is None:
        with _logger_lock:
            if _logger_instance is None:
                _logger_instance = AuditLogger()
    return _logger_instance


def auto_ddl_enabled() -> bool:
    """Whether the server may create its own audit schema.

    Off by default. On a hardened deployment the role is granted only
    SELECT/INSERT and a narrow UPDATE, so the DDL would fail anyway — and
    should, because self-provisioning evidence storage is not a property you
    want in the thing being audited.
    """
    return os.environ.get("MCP_AUDIT_AUTO_DDL", "").strip() in ("1", "true", "yes")


def allow_unaudited_critical_writes() -> bool:
    return os.environ.get("MCP_ALLOW_UNAUDITED_CRITICAL_WRITES", "").strip() in (
        "1",
        "true",
        "yes",
    )


class _Ctx:
    """Handle passed to the body of `audit_call`."""

    def __init__(self) -> None:
        self.summary: str | None = None
        self.committed = False
        self.row_id: int | None = None

    def mark_committed(self) -> None:
        """Call immediately after Odoo returns, before parsing the result.

        Anything that fails after this point failed *around* a write that
        already landed, and must not be replayable.
        """
        self.committed = True


@contextmanager
def audit_call(
    *,
    tool: str,
    instance: str | None,
    params: dict[str, Any] | None,
    session_id: str | None = None,
    idempotency_key: str | None = None,
    actor: str | None = None,
    params_fingerprint: str | None = None,
    critical: bool = False,
):
    """Claim an audit row, run the body, then complete the row.

    Args:
        critical: when True the call is refused if the log is unreachable.
            write_safe tools pass False — they only create drafts, and a
            best-effort record is an acceptable trade there.
        params_fingerprint: fingerprint of the caller's arguments; see `claim`.
        actor: overrides the caller resolved from the access token. Leave
            unset in tools — the token is the trustworthy source, and a
            caller-supplied actor is a caller-supplied audit trail.
    """
    log = get_logger()
    best_effort = not critical or allow_unaudited_critical_writes()

    identity = current_identity()
    ctx = _Ctx()
    ctx.row_id = log.claim(
        tool=tool,
        instance=instance,
        params=params,
        session_id=session_id,
        idempotency_key=idempotency_key,
        actor=actor or identity.actor,
        actor_sub=identity.actor_sub,
        client_id=identity.client_id,
        params_fingerprint=params_fingerprint,
        best_effort=best_effort,
    )

    started = time.monotonic()
    try:
        yield ctx
    except Exception as exc:
        log.complete(
            ctx.row_id,
            status=classify_failure(exc, committed=ctx.committed),
            response_summary=ctx.summary,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise
    else:
        log.complete(
            ctx.row_id,
            status="ok",
            response_summary=ctx.summary,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def find_previous_success(
    *, idempotency_key: str, instance: str, tool: str
) -> dict[str, Any] | None:
    """Look up a prior call that blocks a replay of this (instance, tool, key).

    Raises AuditUnavailable if the check cannot be made — callers must not
    treat an unanswerable question as "no prior call".
    """
    return get_logger().find_successful(
        idempotency_key=idempotency_key, instance=instance, tool=tool,
    )
