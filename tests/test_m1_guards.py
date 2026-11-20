"""M1 guards: staleness, duplicates, errors, evidence labels."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

from invoice_triage.app import MAX_UPLOAD_BYTES

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _ingest(client: TestClient, headers: dict[str, str], name: str) -> dict[str, Any]:
    response = client.post(
        "/ingest",
        files={"file": (name, (FIXTURES / name).read_bytes(), "text/csv")},
        headers=headers,
    )
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def _run(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post("/runs", headers=headers)
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def test_resolve_refuses_stale_version(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    second = _ingest(client, api_headers, "corrected_amount.csv")
    assert second["version"] == 2
    outcome = client.post(
        f"/cases/{failed['case_id']}/resolve", headers=api_headers
    ).json()
    assert (outcome["resolved"], outcome["reason"]) == (False, "stale-version")


def test_duplicate_ingest_and_rerun_keep_single_case(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    first = _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    repeat = _ingest(client, api_headers, "missing_amount.csv")
    assert repeat["duplicate"] is True
    assert repeat["submission_id"] == first["submission_id"]
    assert repeat["version"] == 1
    rerun = _run(client, api_headers)
    assert rerun["case_id"] == failed["case_id"]
    overview = client.get("/", headers=api_headers).json()
    assert len(overview["cases"]) == 1
    assert overview["cases"][0]["status"] == "open"


def test_errored_check_opens_case_and_blocks_resolve(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    """A required check that errors must still open a case with a verdict."""
    import polars as pl
    from airflow_dq_agent.quality.suite import run_suite_on_frames

    import invoice_triage.services as services

    _ingest(client, api_headers, "missing_amount.csv")
    run = services.run_current_checks(
        intake=make_engine(pg_dsns["intake"]),
        read_dsn=pg_dsns["read"],
        _suite_runner=lambda _dsn: run_suite_on_frames(
            {"staging_invoice": pl.DataFrame({"invoice_ref": ["INV-001"]})}
        ),
    )
    assert run["status"] == "error"
    assert run["case_id"] is not None
    case_id = str(run["case_id"])
    case = client.get(f"/cases/{case_id}", headers=api_headers).json()
    assert case["verdict"] == "error"
    assert case["resolution"] == {"allowed": False, "reason": "errored-report"}
    outcome = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (outcome["resolved"], outcome["reason"]) == (False, "errored-report")
    page = client.get(f"/cases/{case_id}")
    assert "Check error" in page.text


def test_oversized_upload_rejected(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    payload = b"invoice_id,supplier_id,invoice_date,currency,amount\n" + b"0" * (
        MAX_UPLOAD_BYTES + 1
    )
    response = client.post(
        "/ingest",
        files={"file": ("huge.csv", payload, "text/csv")},
        headers=api_headers,
    )
    assert response.status_code == 413
    assert client.get("/", headers=api_headers).json()["submissions"] == []


def test_errored_run_cannot_resolve(
    client: TestClient, api_headers: dict[str, str], pg_dsns: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    engine = make_engine(pg_dsns["admin"])
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE wb.case_verdicts SET status = 'error' WHERE run_id = :run"),
            {"run": failed["run_id"]},
        )
    outcome = client.post(
        f"/cases/{failed['case_id']}/resolve", headers=api_headers
    ).json()
    assert (outcome["resolved"], outcome["reason"]) == (False, "errored-report")


def test_missing_audit_event_renders_unknown(
    client: TestClient, api_headers: dict[str, str], pg_dsns: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    engine = make_engine(pg_dsns["admin"])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM dq.traces"))
    case = client.get(f"/cases/{failed['case_id']}", headers=api_headers).json()
    assert case["audit"] == {
        "quality_report": "Unknown",
        "shadow_review": "Unknown",
        "approval": "Unknown",
        "human_decision": "Unknown",
        "quarantine": "Unknown",
        "rejection": "Unknown",
        "timeout": "Unknown",
    }


def test_case_audit_labels_truthful(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    case = client.get(f"/cases/{failed['case_id']}", headers=api_headers).json()
    assert case["audit"] == {
        "quality_report": "recorded",
        "shadow_review": "Unknown",
        "approval": "Unknown",
        "human_decision": "Unknown",
        "quarantine": "Unknown",
        "rejection": "Unknown",
        "timeout": "Unknown",
    }
    assert "quarantine" not in case["status"]


def test_rejected_csv_returns_400_and_persists_nothing(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    response = client.post(
        "/ingest",
        files={
            "file": (
                "bad_amount.csv",
                (FIXTURES / "bad_amount.csv").read_bytes(),
                "text/csv",
            )
        },
        headers=api_headers,
    )
    assert response.status_code == 400
    overview = client.get("/", headers=api_headers).json()
    assert overview["submissions"] == []
    assert overview["cases"] == []
    assert client.post("/runs", headers=api_headers).status_code == 409


def test_run_without_case_shows_run_identity_in_html(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    response = client.post(
        "/ingest",
        files={
            "file": (
                "valid_control.csv",
                (FIXTURES / "valid_control.csv").read_bytes(),
                "text/csv",
            )
        },
        headers=api_headers,
    )
    assert response.status_code == 200
    page = client.post("/runs", follow_redirects=True)
    assert page.status_code == 200
    assert re.search(r"Last run ([0-9a-f]{32}): pass", page.text) is not None
