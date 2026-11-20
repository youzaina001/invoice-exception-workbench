"""Concurrent first-run registration and governance adapter checks."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest
from airflow_dq_agent.contracts.models import QualitySuiteReport
from airflow_dq_agent.contracts.tables import (
    TABLE_CONTRACTS,
    TableContract,
    register_contract,
)
from airflow_dq_agent.quality.registry import CHECK_SPECS
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import invoice_triage.governance as registration


def test_concurrent_first_runs_register_once(
    client: TestClient, api_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pause the external core registration boundary to overlap two HTTP runs."""
    fixture = Path(__file__).resolve().parent.parent / "fixtures/valid_control.csv"
    assert (
        client.post(
            "/ingest",
            files={"file": (fixture.name, fixture.read_bytes(), "text/csv")},
            headers=api_headers,
        ).status_code
        == 200
    )
    monkeypatch.delitem(TABLE_CONTRACTS, registration.STAGING_TABLE)
    monkeypatch.delitem(CHECK_SPECS, registration.AMOUNT_CHECK_ID)
    barrier = Barrier(2)

    def overlapping_register(contract: TableContract) -> None:
        try:
            barrier.wait(timeout=1)
        except BrokenBarrierError:
            pass
        register_contract(contract)

    monkeypatch.setattr(registration, "register_contract", overlapping_register)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.post, "/runs", headers=api_headers) for _ in range(2)
        ]
        responses = [future.result(timeout=15) for future in futures]
    assert [response.status_code for response in responses] == [200, 200]
    assert all(response.json()["status"] == "pass" for response in responses)


def test_read_audit_lineage_for_attempt_fails_open_on_sqlalchemy_error(
    pg_dsns: dict[str, str],
) -> None:
    intake_engine = create_engine(pg_dsns["intake"], future=True, pool_pre_ping=True)
    try:
        view = registration.read_audit_lineage(intake_engine, attempt_id="any-attempt")
    finally:
        intake_engine.dispose()
    assert view.report_available is False
    assert view.events == []
    assert view.labels["quality_report"] == "Unknown"


def test_execute_amount_check_raises_when_required_check_missing(
    pg_dsns: dict[str, str],
) -> None:
    with pytest.raises(
        registration.CheckMissingError, match=registration.AMOUNT_CHECK_ID
    ):
        registration.execute_amount_check(
            pg_dsns["read"],
            _suite_runner=lambda _dsn: QualitySuiteReport(checks=[]),
        )


def test_configure_governance_sets_env_and_returns_previous(tmp_path: Path) -> None:
    previous = registration.configure_governance(
        audit_dsn="postgresql+psycopg://example/audit",
        traces_dir=tmp_path,
    )
    try:
        assert os.environ["TRACE_POSTGRES"] == "true"
        assert os.environ["AUDIT_DSN"] == "postgresql+psycopg://example/audit"
        assert os.environ["LLM_MODE"] == "stub"
        assert os.environ["APPLY_MODE"] == "off"
        assert os.environ["TRACES_DIR"] == str(tmp_path)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
