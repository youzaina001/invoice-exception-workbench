"""Staging holds exactly the current submission across invoices."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

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


def test_staging_holds_only_current_submission(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    assert failed["status"] == "fail"
    case_id = failed["case_id"]

    _ingest(client, api_headers, "valid_control.csv")
    passed = _run(client, api_headers)
    assert (passed["status"], passed["case_id"]) == ("pass", None)
    overview = client.get("/", headers=api_headers).json()
    assert [c["id"] for c in overview["cases"]] == [case_id]

    displaced = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (displaced["resolved"], displaced["reason"]) == (False, "current-missing")

    _ingest(client, api_headers, "corrected_amount.csv")
    repassed = _run(client, api_headers)
    assert (repassed["status"], repassed["case_id"]) == ("pass", case_id)
    resolved = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
    case = client.get(f"/cases/{case_id}", headers=api_headers).json()
    amounts = [s["row"]["amount_minor"] for s in case["history"]["submissions"]]
    assert amounts == [None, 12345]


def test_duplicate_reupload_restages_latest_submission(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    case_id = failed["case_id"]
    second = _ingest(client, api_headers, "corrected_amount.csv")
    _ingest(client, api_headers, "valid_control.csv")
    displaced = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (displaced["resolved"], displaced["reason"]) == (
        False,
        "current-missing",
    )

    repeat = _ingest(client, api_headers, "corrected_amount.csv")
    assert repeat["duplicate"] is True
    assert repeat["submission_id"] == second["submission_id"]
    assert repeat["version"] == 2
    rerun = _run(client, api_headers)
    assert (rerun["invoice_ref"], rerun["version"]) == ("INV-001", 2)
    assert (rerun["status"], rerun["case_id"]) == ("pass", case_id)
    resolved = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")


def test_duplicate_of_older_version_keeps_current_staging(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    _run(client, api_headers)
    _ingest(client, api_headers, "corrected_amount.csv")
    repeat = _ingest(client, api_headers, "missing_amount.csv")
    assert (repeat["duplicate"], repeat["version"]) == (True, 1)
    rerun = _run(client, api_headers)
    assert (rerun["invoice_ref"], rerun["version"]) == ("INV-001", 2)


def test_duplicate_ingest_html_shows_notice(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "corrected_amount.csv")
    page = client.post(
        "/ingest",
        files={
            "file": (
                "corrected_amount.csv",
                (FIXTURES / "corrected_amount.csv").read_bytes(),
                "text/csv",
            )
        },
        follow_redirects=True,
    )
    assert page.status_code == 200
    assert "Duplicate upload" in page.text
    assert "INV-001" in page.text
    assert "restaged as current" in page.text
