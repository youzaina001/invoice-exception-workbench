"""Case evidence through HTTP over real Postgres and installed core."""

from pathlib import Path

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from invoice_triage.app import create_app
from invoice_triage.settings import Settings

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
    assert case["audit"]["quality_report"] == "Unknown"
    assert case["resolution"] == {"allowed": False, "reason": "incomplete-report"}
    outcome = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert outcome["resolved"] is False
    assert outcome["reason"] == "incomplete-report"
    assert (
        client.get(f"/cases/{case_id}", headers=api_headers).json()["status"] == "open"
    )


@pytest.mark.parametrize(
    ("kind", "label", "expected"),
    [
        ("approval_review", "shadow_review", "Review recorded (mode Unknown)"),
        ("human_approved", "approval", "Approved"),
        ("human_rejected", "rejection", "Rejected"),
        ("human_timed_out", "timeout", "Timed out"),
        ("apply_succeeded", "quarantine", "Copy recorded"),
    ],
)
def test_associated_audit_events_are_visible_without_resolving_case(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
    kind: str,
    label: str,
    expected: str,
) -> None:
    """Stored core events test read/display only, not a live Airflow journey."""
    from airflow_dq_agent.contracts.models import AuditEvent
    from airflow_dq_agent.traces.writer import PostgresAuditSink

    client.post(
        "/ingest",
        files={"file": ("missing.csv", (FIXTURES / "missing_amount.csv").read_bytes())},
        headers=api_headers,
    ).raise_for_status()
    run = client.post("/runs", headers=api_headers).json()
    sink = PostgresAuditSink(pg_dsns["audit"])
    payload = {
        "kind": kind,
        "quality_run_id": run["run_id"],
        "fingerprint": "synthetic-read-fixture",
        "apply_steps": [
            {"action_id": "quarantine_nulls", "table": "staging_invoice", "rowcount": 1}
        ]
        if kind == "apply_succeeded"
        else [],
    }
    event = AuditEvent.model_validate(payload)
    sink.append(event)
    url = f"/cases/{run['case_id']}"
    case = client.get(url, headers=api_headers).json()
    assert case["audit"][label] == expected
    assert case["status"] == "open"
    assert (
        client.post(f"{url}/resolve", headers=api_headers).json()["resolved"] is False
    )
    assert expected in client.get(url).text

    # The same kind on another run must not replace this case's evidence.
    sink.append(
        event.model_copy(
            update={"event_id": "unrelated", "quality_run_id": "other-run"}
        )
    )
    assert client.get(url, headers=api_headers).json()["audit"][label] == expected

    client.post(
        "/ingest",
        files={
            "file": ("corrected.csv", (FIXTURES / "corrected_amount.csv").read_bytes())
        },
        headers=api_headers,
    ).raise_for_status()
    client.post("/runs", headers=api_headers).raise_for_status()
    corrected = client.get(url, headers=api_headers).json()
    assert corrected["audit"][label] == "Unknown"
    assert event.event_id in {
        item["event_id"] for item in corrected["history"]["audit"]
    }
    assert "unrelated" not in {
        item["event_id"] for item in corrected["history"]["audit"]
    }
    assert kind.replace("_", " ").capitalize() in client.get(url).text
