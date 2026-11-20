"""ExecutionScope, resolve_scope, scoped_transaction, and workspace views."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from invoice_triage import request_scope


def _scoped_engine(dsn: str) -> Engine:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    request_scope.install_scope(engine)
    return engine


def test_resolve_scope_local_and_hosted_six_attempt_rotation(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    audit_engine = _scoped_engine(pg_dsns["audit"])
    try:
        assert (
            request_scope.resolve_scope(engine, hosted=False)
            == request_scope.ExecutionScope.local()
        )
        first = request_scope.resolve_scope(
            engine, hosted=True, reviewer_id="scope-rot"
        )
        assert first.hosted is True
        assert first.reviewer_id == "scope-rot"
        assert first.attempt_id is not None
        assert (
            request_scope.resolve_scope(engine, hosted=True, reviewer_id="scope-rot")
            == first
        )

        current = first
        for _ in range(5):
            nxt = request_scope.resolve_scope(
                engine, hosted=True, reviewer_id="scope-rot", start_fresh=True
            )
            assert nxt.previous_attempt_id == current.attempt_id
            assert nxt.attempt_id is not None
            current = nxt

        view = request_scope.read_workspace_view(engine, scope=current)
        assert len(view["attempts"]) == 5
        assert first.attempt_id not in {item["id"] for item in view["attempts"]}
        with pytest.raises(request_scope.AttemptNotFoundError):
            request_scope.read_workspace_view(
                engine,
                audit=audit_engine,
                scope=current,
                attempt_id=first.attempt_id,
            )
    finally:
        engine.dispose()
        audit_engine.dispose()


def test_scoped_transaction_unifies_read_only_and_local_only_guards(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        first = request_scope.resolve_scope(
            engine, hosted=True, reviewer_id="scope-guard"
        )
        current = request_scope.resolve_scope(
            engine, hosted=True, reviewer_id="scope-guard", start_fresh=True
        )
        stale_header = request_scope.resolve_scope(
            engine,
            hosted=True,
            reviewer_id="scope-guard",
            requested_attempt_id=first.attempt_id,
        )
        assert stale_header.is_read_only_attempt is True

        with pytest.raises(
            request_scope.AttemptReadOnlyError, match="attempt-read-only"
        ):
            with request_scope.scoped_transaction(engine, stale_header, mutate=True):
                pass

        with pytest.raises(
            request_scope.AttemptReadOnlyError, match="attempt-read-only"
        ):
            with request_scope.scoped_transaction(engine, first, mutate=True):
                pass

        with pytest.raises(PermissionError):
            with request_scope.scoped_transaction(engine, current, local_only=True):
                pass
    finally:
        engine.dispose()


def test_read_workspace_view_audit_fails_open_on_sqlalchemy_error(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        scope = request_scope.resolve_scope(
            engine, hosted=True, reviewer_id="scope-audit-err"
        )
        view = request_scope.read_workspace_view(
            engine, audit=engine, scope=scope, attempt_id=scope.attempt_id
        )
        assert view["attempt_id"] == scope.attempt_id
        assert view["audit"] == []
    finally:
        engine.dispose()
