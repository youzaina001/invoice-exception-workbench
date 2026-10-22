"""Transaction-local Reviewer scope for hosted requests.

The setting is not session-wide: a pooled connection must not keep a Reviewer
after commit. Local mode leaves the settings unset so existing rows stay visible.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

_hosted: ContextVar[bool] = ContextVar("ledgerlens_hosted", default=False)
_reviewer: ContextVar[str | None] = ContextVar("ledgerlens_reviewer", default=None)
_attempt: ContextVar[str | None] = ContextVar("ledgerlens_attempt", default=None)


@dataclass(frozen=True)
class ScopeTokens:
    hosted: Token[bool]
    reviewer: Token[str | None]
    attempt: Token[str | None]


def bind_scope(*, hosted: bool, reviewer: str | None) -> ScopeTokens:
    return ScopeTokens(
        hosted=_hosted.set(hosted),
        reviewer=_reviewer.set(reviewer),
        attempt=_attempt.set(None),
    )


def set_attempt(attempt_id: str | None) -> None:
    _attempt.set(attempt_id)


def current_hosted() -> bool:
    return _hosted.get()


def current_reviewer() -> str | None:
    return _reviewer.get()


def current_attempt() -> str | None:
    return _attempt.get()


def reset_scope(tokens: ScopeTokens) -> None:
    _attempt.reset(tokens.attempt)
    _reviewer.reset(tokens.reviewer)
    _hosted.reset(tokens.hosted)


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
    if conn.info.get("_ledgerlens_scope_applied") or not _hosted.get():
        return
    # Set before the nested set_config statements so they do not re-enter.
    conn.info["_ledgerlens_scope_applied"] = True
    conn.exec_driver_sql("SELECT set_config('app.hosted', 'true', true)")
    reviewer = _reviewer.get()
    if reviewer:
        conn.exec_driver_sql(
            "SELECT set_config('app.reviewer', %(reviewer)s, true)",
            {"reviewer": reviewer},
        )
    attempt = _attempt.get()
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


def activate_connection_scope() -> None:
    """Stamp Reviewer settings on engines the quality core opens itself."""
    import airflow_dq_agent.quality.suite as suite
    import airflow_dq_agent.traces.writer as writer
    import airflow_dq_agent.warehouse.db as warehouse_db

    if getattr(warehouse_db.make_engine, "_ledgerlens_scoped", False):
        return
    original = warehouse_db.make_engine

    def make_engine(dsn: str | None = None) -> Engine:
        engine = original(dsn)
        install_scope(engine)
        return engine

    setattr(make_engine, "_ledgerlens_scoped", True)
    setattr(warehouse_db, "make_engine", make_engine)
    setattr(suite, "make_engine", make_engine)
    setattr(writer, "make_engine", make_engine)
