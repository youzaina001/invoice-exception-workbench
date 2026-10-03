"""M1 fixtures: real embedded Postgres + installed core, reset between tests."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator

import pytest
from airflow_dq_agent.adoption import (
    apply_governance_schema,
    provision_restricted_logins,
)
from airflow_dq_agent.warehouse.db import make_engine
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from ledgerlens.app import create_app
from ledgerlens.core_integration import ensure_registered
from ledgerlens.db import setup_database
from ledgerlens.local_pg import LocalPostgres, find_free_port
from ledgerlens.settings import Settings


@pytest.fixture(scope="session")
def pg_dsns(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    tmp = tmp_path_factory.mktemp("pg")
    pg = LocalPostgres(pgdata=tmp / "data", port=find_free_port())
    pg.start()
    try:
        pg.ensure_database("warehouse")
        admin_wh = pg.dsn(db="warehouse")
        apply_governance_schema(admin_wh)
        creds = provision_restricted_logins(admin_wh)
        intake = setup_database(admin_wh)
        traces = tmp_path_factory.mktemp("traces")
        os.environ["TRACE_POSTGRES"] = "true"
        os.environ["AUDIT_DSN"] = creds.audit_dsn
        os.environ["LLM_MODE"] = "stub"
        os.environ["APPLY_MODE"] = "off"
        os.environ["TRACES_DIR"] = str(traces)
        ensure_registered()
        yield {
            "admin": admin_wh,
            "intake": intake.intake_dsn,
            "read": creds.read_dsn,
            "audit": creds.audit_dsn,
        }
    finally:
        pg.stop()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_db(pg_dsns: dict[str, str]) -> Iterator[None]:
    """Truncate test tables between tests. Test harness only; never runtime."""
    engine = make_engine(pg_dsns["admin"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE wb.case_verdicts, wb.cases, wb.submission_rows, "
                "wb.submissions, wb.staging_invoice, wb.demo_attempts, "
                "wb.demo_workspaces, dq.check_runs, dq.traces"
            )
        )
    yield


@pytest.fixture()
def client(pg_dsns: dict[str, str]) -> TestClient:
    settings = Settings(
        intake_dsn=pg_dsns["intake"],
        read_dsn=pg_dsns["read"],
        audit_dsn=pg_dsns["audit"],
    )
    return TestClient(create_app(settings))


@pytest.fixture()
def api_headers() -> dict[str, str]:
    return {"Accept": "application/json"}


def signed_in_reviewer(request: Request) -> str | None:
    """Test-only stand-in for the externally verified Clerk session."""
    return (request.headers.get("X-Reviewer") or "").strip() or None
