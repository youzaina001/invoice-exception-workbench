"""Case evidence through HTTP over real Postgres and installed core."""

from pathlib import Path

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from ledgerlens.app import create_app
from ledgerlens.settings import Settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_case_retains_bounded_core_failure_samples_after_correction(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    uploaded = client.post(
        "/ingest",
        files={"file": ("missing.csv", (FIXTURES / "missing_amount.csv").read_bytes())},
        headers=api_headers,
    ).json()
    run = client.post("/runs", headers=api_headers).json()
    case_id = run["case_id"]
    case = client.get(f"/cases/{case_id}", headers=api_headers).json()
    samples = case["core"]["sample_failures"]
    assert len(samples) == 1
    assert samples[0]["submission_id"] == uploaded["submission_id"]
    assert samples[0]["invoice_id"] == "INV-001"
    assert samples[0]["supplier_id"] == "SUP-42"
    assert samples[0]["invoice_date"] == "2026-09-01"
    assert samples[0]["currency"] == "EUR"
    assert samples[0]["amount_minor"] is None
    assert len(samples) <= 20

    client.post(
        "/ingest",
        files={
            "file": ("corrected.csv", (FIXTURES / "corrected_amount.csv").read_bytes())
        },
        headers=api_headers,
    ).raise_for_status()
    client.post("/runs", headers=api_headers).raise_for_status()
    corrected = client.get(f"/cases/{case_id}", headers=api_headers).json()
    assert corrected["core"]["sample_failures"] == []
    assert corrected["history"]["verdicts"][0]["sample_failures"] == samples


@pytest.mark.parametrize(
    "loss", ["deleted", "unreadable", "malformed", "misbound", "missing-id"]
)
def test_unavailable_audit_evidence_blocks_resolution(
    client: TestClient, api_headers: dict[str, str], pg_dsns: dict[str, str], loss: str
) -> None:
    for name in ("missing_amount.csv", "corrected_amount.csv"):
        client.post(
            "/ingest",
            files={"file": (name, (FIXTURES / name).read_bytes())},
            headers=api_headers,
        ).raise_for_status()
        run = client.post("/runs", headers=api_headers).json()
    case_id = run["case_id"]
    if loss == "unreadable":
        # Intake credentials intentionally cannot access dq.*. This exercises
        # real read failure without mocking the audit adapter or its caller.
        client = TestClient(
            create_app(
                Settings(
                    warehouse_dsn=pg_dsns["read"],
                    intake_dsn=pg_dsns["intake"],
                    read_dsn=pg_dsns["read"],
                    audit_dsn=pg_dsns["intake"],
                )
            )
        )
    else:
        faults = {
            "deleted": "DELETE FROM dq.traces",
            "malformed": "UPDATE dq.traces SET body = '{}'::jsonb",
            "misbound": "UPDATE wb.case_verdicts SET audit_event_id = "
            "(SELECT audit_event_id FROM wb.case_verdicts ORDER BY id LIMIT 1)",
            "missing-id": "UPDATE wb.case_verdicts SET audit_event_id = NULL",
        }
        with make_engine(pg_dsns["admin"]).begin() as conn:
            conn.execute(text(faults[loss]))
    case = client.get(f"/cases/{case_id}", headers=api_headers).json()
    assert case["audit"]["quality_report"] == "unknown"
    assert case["resolution"] == {"allowed": False, "reason": "incomplete-report"}
    outcome = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert outcome["resolved"] is False
    assert outcome["reason"] == "incomplete-report"
    assert (
        client.get(f"/cases/{case_id}", headers=api_headers).json()["status"] == "open"
    )
