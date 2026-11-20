"""Explicit execution scope, transaction-local RLS, and Demo Attempt lifecycle.

The setting is not session-wide: a pooled connection must not keep a Reviewer
after commit. Local mode leaves the settings unset so existing rows stay visible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine

from invoice_triage.governance import read_audit_lineage

_MAX_DEMO_ATTEMPTS = 5
# Keep lock identifiers stable across package renames so running versions agree.
_STAGING_LOCK_KEY = (
    int.from_bytes(hashlib.sha256(b"ledgerlens:staging").digest()[:8], "big") >> 1
)

_active_scope: ContextVar[tuple[bool, str | None, str | None] | None] = ContextVar(
    "ledgerlens_scope", default=None
)


class AttemptNotFoundError(RuntimeError):
    pass


class AttemptReadOnlyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionScope:
    hosted: bool = False
    reviewer_id: str | None = None
    workspace_id: str | None = None
    attempt_id: str | None = None
    previous_attempt_id: str | None = None
    requested_attempt_id: str | None = None

    @classmethod
    def local(cls) -> ExecutionScope:
        return cls()

    @property
    def is_read_only_attempt(self) -> bool:
        return (
            self.hosted
            and self.requested_attempt_id is not None
            and self.attempt_id is not None
            and self.requested_attempt_id != self.attempt_id
        )


@contextmanager
def _bind_connection_scope(
    *, hosted: bool, reviewer_id: str | None, attempt_id: str | None
) -> Iterator[None]:
    token = _active_scope.set((hosted, reviewer_id, attempt_id))
    try:
        yield
    finally:
        _active_scope.reset(token)


def _clear_scope_flag(conn: Connection) -> None:
    conn.info.pop("_ledgerlens_scope_applied", None)


def _apply_scope(
    conn: Connection,
    cursor: object,
    statement: str,
    parameters: object,
    context: object,
    executemany: bool,
) -> None:
    del cursor, statement, parameters, context, executemany
    active = _active_scope.get()
    if active is None or conn.info.get("_ledgerlens_scope_applied"):
        return
    hosted, reviewer, attempt = active
    if not hosted:
        return
    # Set before the nested set_config statements so they do not re-enter.
    conn.info["_ledgerlens_scope_applied"] = True
    conn.exec_driver_sql("SELECT set_config('app.hosted', 'true', true)")
    if reviewer:
        conn.exec_driver_sql(
            "SELECT set_config('app.reviewer', %(reviewer)s, true)",
            {"reviewer": reviewer},
        )
    if attempt:
        conn.exec_driver_sql(
            "SELECT set_config('app.attempt', %(attempt)s, true)",
            {"attempt": attempt},
        )


def install_scope(engine: Engine) -> None:
    if engine.__dict__.get("_ledgerlens_scope"):
        return
    engine.__dict__["_ledgerlens_scope"] = True
    event.listen(engine, "begin", _clear_scope_flag)
    event.listen(engine, "commit", _clear_scope_flag)
    event.listen(engine, "rollback", _clear_scope_flag)
    event.listen(engine, "before_cursor_execute", _apply_scope)


def acquire_staging_lock(conn: Connection) -> None:
    # Transaction-level advisory locks are re-entrant, so locking again inside
    # one transaction is harmless; caching it on the pooled connection is not.
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGING_LOCK_KEY})


def _lock_workspace(conn: Connection, reviewer_id: str) -> None:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:reviewer, 0))"),
        {"reviewer": "ledgerlens:workspace:" + reviewer_id},
    )


def _delete_demo_attempt(conn: Connection, attempt_id: str) -> None:
    """Remove one attempt and the rows that belong only to it.

    Audit tables are append-only for the request role, so the check runs and
    traces go through a definer function that deletes by this attempt only.
    """
    params = {"attempt": attempt_id}
    for statement in (
        "DELETE FROM wb.case_verdicts WHERE attempt_id = :attempt",
        "DELETE FROM wb.cases WHERE attempt_id = :attempt",
        "DELETE FROM wb.submission_rows WHERE attempt_id = :attempt",
        "DELETE FROM wb.staging_invoice WHERE attempt_id = :attempt",
        "DELETE FROM wb.submissions WHERE attempt_id = :attempt",
    ):
        conn.execute(text(statement), params)
    conn.execute(text("SELECT wb.delete_attempt_audit(:attempt)"), params)
    conn.execute(text("DELETE FROM wb.demo_attempts WHERE id = :attempt"), params)


def _read_overview_rows(conn: Connection, attempt_id: str | None) -> dict[str, Any]:
    submission_sql = (
        "SELECT id, invoice_ref, version, filename, ingested_at FROM wb.submissions"
    )
    case_sql = "SELECT id, invoice_ref, check_id, status, created_at FROM wb.cases"
    params: dict[str, str] = {}
    if attempt_id is not None:
        submission_sql += " WHERE attempt_id = :attempt"
        case_sql += " WHERE attempt_id = :attempt"
        params["attempt"] = attempt_id
    submissions = (
        conn.execute(
            text(submission_sql + " ORDER BY ingested_at DESC"),
            params,
        )
        .mappings()
        .all()
    )
    cases = (
        conn.execute(text(case_sql + " ORDER BY created_at DESC"), params)
        .mappings()
        .all()
    )
    return {
        "submissions": [
            {
                "id": r["id"],
                "invoice_ref": r["invoice_ref"],
                "version": r["version"],
                "filename": r["filename"],
                "ingested_at": r["ingested_at"].isoformat(),
            }
            for r in submissions
        ],
        "cases": [
            {
                "id": r["id"],
                "invoice_ref": r["invoice_ref"],
                "check_id": r["check_id"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in cases
        ],
    }


def resolve_scope(
    engine: Engine,
    *,
    hosted: bool,
    reviewer_id: str | None = None,
    requested_attempt_id: str | None = None,
    start_fresh: bool = False,
) -> ExecutionScope:
    """Resolve the active local or hosted Demo Attempt scope."""
    if not hosted:
        return ExecutionScope.local()
    if not reviewer_id:
        raise PermissionError("sign-in required")
    with (
        _bind_connection_scope(hosted=True, reviewer_id=reviewer_id, attempt_id=None),
        engine.begin() as conn,
    ):
        _lock_workspace(conn, reviewer_id)
        existing = (
            conn.execute(
                text("SELECT id FROM wb.demo_workspaces WHERE reviewer_id = :r"),
                {"r": reviewer_id},
            )
            .mappings()
            .first()
        )
        if existing is None:
            workspace_id = uuid4().hex
            conn.execute(
                text(
                    "INSERT INTO wb.demo_workspaces (id, reviewer_id) VALUES (:id, :r)"
                ),
                {"id": workspace_id, "r": reviewer_id},
            )
        else:
            workspace_id = str(existing["id"])
        attempts = (
            conn.execute(
                text(
                    "SELECT id FROM wb.demo_attempts WHERE workspace_id = :w"
                    " ORDER BY created_at ASC, id ASC"
                ),
                {"w": workspace_id},
            )
            .mappings()
            .all()
        )
        if not start_fresh:
            if not attempts:
                attempt_id = uuid4().hex
                conn.execute(
                    text(
                        "INSERT INTO wb.demo_attempts"
                        " (id, workspace_id, reviewer_id, created_at)"
                        " VALUES (:id, :w, :r, clock_timestamp())"
                    ),
                    {"id": attempt_id, "w": workspace_id, "r": reviewer_id},
                )
            else:
                attempt_id = str(attempts[-1]["id"])
            return ExecutionScope(
                hosted=True,
                reviewer_id=reviewer_id,
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                requested_attempt_id=requested_attempt_id,
            )
        previous_id = str(attempts[-1]["id"]) if attempts else None
        attempt_id = uuid4().hex
        conn.execute(
            text(
                "INSERT INTO wb.demo_attempts"
                " (id, workspace_id, reviewer_id, created_at)"
                " VALUES (:id, :w, :r, clock_timestamp())"
            ),
            {"id": attempt_id, "w": workspace_id, "r": reviewer_id},
        )
        if len(attempts) >= _MAX_DEMO_ATTEMPTS:
            _delete_demo_attempt(conn, str(attempts[0]["id"]))
        return ExecutionScope(
            hosted=True,
            reviewer_id=reviewer_id,
            workspace_id=workspace_id,
            attempt_id=attempt_id,
            previous_attempt_id=previous_id,
        )


@contextmanager
def scoped_transaction(
    engine: Engine,
    scope: ExecutionScope | None = None,
    *,
    mutate: bool = False,
    lock_staging: bool = False,
    local_only: bool = False,
) -> Iterator[Connection]:
    """Open a connection or transaction bound to an explicit ExecutionScope."""
    resolved = scope or ExecutionScope.local()
    if local_only and resolved.hosted:
        raise PermissionError("PDF review is available locally only")
    if mutate and resolved.is_read_only_attempt:
        raise AttemptReadOnlyError("attempt-read-only")
    attempt_for_rls = resolved.attempt_id if mutate else None
    with _bind_connection_scope(
        hosted=resolved.hosted,
        reviewer_id=resolved.reviewer_id,
        attempt_id=attempt_for_rls,
    ):
        if mutate:
            with engine.begin() as conn:
                if resolved.hosted:
                    if not resolved.reviewer_id or not resolved.attempt_id:
                        raise AttemptReadOnlyError("attempt-read-only")
                    _lock_workspace(conn, resolved.reviewer_id)
                    latest = conn.execute(
                        text(
                            "SELECT id FROM wb.demo_attempts WHERE reviewer_id = :r"
                            " ORDER BY created_at DESC, id DESC LIMIT 1"
                        ),
                        {"r": resolved.reviewer_id},
                    ).scalar_one_or_none()
                    if latest != resolved.attempt_id:
                        raise AttemptReadOnlyError("attempt-read-only")
                if lock_staging:
                    acquire_staging_lock(conn)
                yield conn
        else:
            with engine.connect() as conn:
                yield conn


def read_workspace_view(
    intake: Engine,
    *,
    audit: Engine | None = None,
    scope: ExecutionScope | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Read overview rows and attempt history or one specific Demo Attempt."""
    resolved = scope or ExecutionScope.local()
    with scoped_transaction(intake, resolved, mutate=False) as conn:
        if attempt_id is None:
            overview = _read_overview_rows(conn, resolved.attempt_id)
            if resolved.hosted:
                overview["workspace_id"] = resolved.workspace_id
                overview["attempt_id"] = resolved.attempt_id
                attempts = [
                    {"id": row.id, "created_at": row.created_at.isoformat()}
                    for row in conn.execute(
                        text(
                            "SELECT id, created_at FROM wb.demo_attempts"
                            " ORDER BY created_at DESC, id DESC"
                        )
                    )
                ]
            else:
                attempts = []
            return {"overview": overview, "attempts": attempts}

        attempt = (
            conn.execute(
                text("SELECT id, workspace_id FROM wb.demo_attempts WHERE id = :id"),
                {"id": attempt_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise AttemptNotFoundError(f"attempt {attempt_id} not found")
        overview = _read_overview_rows(conn, attempt_id)
        verdicts = (
            conn.execute(
                text(
                    "SELECT case_id, run_id, check_id, status"
                    " FROM wb.case_verdicts WHERE attempt_id = :attempt"
                    " ORDER BY id"
                ),
                {"attempt": attempt_id},
            )
            .mappings()
            .all()
        )
        audit_events = (
            read_audit_lineage(audit, attempt_id=attempt_id).events
            if audit is not None
            else []
        )
        return {
            "workspace_id": str(attempt["workspace_id"]),
            "attempt_id": str(attempt["id"]),
            **overview,
            "check_results": [
                {
                    "case_id": row["case_id"],
                    "run_id": row["run_id"],
                    "check_id": row["check_id"],
                    "status": row["status"],
                }
                for row in verdicts
            ],
            "audit": audit_events,
        }
