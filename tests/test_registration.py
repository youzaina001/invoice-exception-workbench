"""Concurrent first-run registration through the HTTP interface."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest
from airflow_dq_agent.contracts.tables import (
    TABLE_CONTRACTS,
    TableContract,
    register_contract,
)
from airflow_dq_agent.quality.registry import CHECK_SPECS
from fastapi.testclient import TestClient

import ledgerlens.core_integration as registration


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
