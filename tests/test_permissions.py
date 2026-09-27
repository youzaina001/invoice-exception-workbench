"""Least-privilege access and no evidence leak into core audit."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SENSITIVE_STRINGS = ("INV-001", "SUP-42", "EUR", "2026-09-01")
SENSITIVE_INT = 12345


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def _denied(dsn: str, sql: str) -> None:
    """One denied statement per transaction: a refusal aborts its own transaction."""
    engine = make_engine(dsn)
    with engine.begin() as conn:
        with pytest.raises(DBAPIError) as exc:
            conn.execute(text(sql))
    assert isinstance(exc.value.orig, InsufficientPrivilege)


def test_read_login_cannot_write(pg_dsns: dict[str, str]) -> None:
    engine = make_engine(pg_dsns["read"])
    with engine.begin() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM wb.staging_invoice")).first()
        assert row is not None
    _denied(
        pg_dsns["read"],
        "INSERT INTO wb.staging_invoice VALUES ('x','x',1,'x','x',NOW(),'x',1,'x')",
    )
    _denied(
        pg_dsns["read"],
        "INSERT INTO dq.check_runs (run_id, check_id, status, n_failed, n_total, body)"
        " VALUES ('x','x','pass',0,0,'{}')",
    )
    _denied(pg_dsns["read"], "SELECT COUNT(*) FROM wb.submissions")


def test_intake_cannot_touch_core_audit(pg_dsns: dict[str, str]) -> None:
    _denied(pg_dsns["intake"], "SELECT COUNT(*) FROM dq.traces")
    _denied(
        pg_dsns["intake"],
        "INSERT INTO dq.traces (trace_id, kind, body) VALUES ('x','x','{}')",
    )


def test_audit_login_cannot_read_companion(pg_dsns: dict[str, str]) -> None:
    engine = make_engine(pg_dsns["audit"])
    with engine.begin() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM dq.traces")).first()
        assert row is not None
    _denied(pg_dsns["audit"], "SELECT COUNT(*) FROM wb.submissions")


def test_core_audit_contains_no_row_values(
    client: TestClient, api_headers: dict[str, str], pg_dsns: dict[str, str]
) -> None:
    for name in ("missing_amount.csv", "corrected_amount.csv"):
        response = client.post(
            "/ingest",
            files={"file": (name, (FIXTURES / name).read_bytes(), "text/csv")},
            headers=api_headers,
        )
        assert response.status_code == 200
        assert client.post("/runs", headers=api_headers).status_code == 200
    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        bodies = [
            row[0] for row in conn.execute(text("SELECT body FROM dq.check_runs")).all()
        ] + [row[0] for row in conn.execute(text("SELECT body FROM dq.traces")).all()]
    assert len(bodies) > 0
    values = [leaf for body in bodies for leaf in _walk(body)]
    texts = [leaf for leaf in values if isinstance(leaf, str)]
    assert not any(any(s in leaf for leaf in texts) for s in SENSITIVE_STRINGS)
    numbers = [
        Decimal(str(leaf))
        for leaf in values
        if isinstance(leaf, (int, float, Decimal)) and not isinstance(leaf, bool)
    ]
    assert Decimal(SENSITIVE_INT) not in numbers
    assert Decimal("123.45") not in numbers
    assert not any(leaf in ("12345", "123.45") for leaf in texts)
