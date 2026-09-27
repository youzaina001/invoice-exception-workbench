"""Resolve journey: corrected resubmission, gated Resolve, valid control."""

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


def test_corrected_resubmission_resolves_only_via_explicit_resolve(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    first = _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    assert failed["status"] == "fail"
    case_id = failed["case_id"]

    second = _ingest(client, api_headers, "corrected_amount.csv")
    assert (second["version"], second["duplicate"]) == (2, False)
    passed = _run(client, api_headers)
    assert passed["status"] == "pass"
    assert (passed["case_id"], passed["case_status"]) == (case_id, "open")

    case = client.get(f"/cases/{case_id}", headers=api_headers).json()
    assert case["status"] == "open"
    assert case["submission_id"] == first["submission_id"]
    amounts = [s["row"]["amount_minor"] for s in case["history"]["submissions"]]
    assert amounts == [None, 12345]

    resolved = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
    assert resolved["resolved_run_id"] == passed["run_id"]

    assert (
        client.get(f"/cases/{case_id}", headers=api_headers).json()["status"]
        == "resolved"
    )

    again = client.post(f"/cases/{case_id}/resolve", headers=api_headers).json()
    assert (again["resolved"], again["reason"]) == (False, "case-not-open")


def test_resolve_refuses_failed_verdict(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    outcome = client.post(
        f"/cases/{failed['case_id']}/resolve", headers=api_headers
    ).json()
    assert (outcome["resolved"], outcome["reason"]) == (False, "verdict-not-pass")
    case = client.get(f"/cases/{failed['case_id']}", headers=api_headers).json()
    assert case["status"] == "open"


def test_valid_control_creates_no_case(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "valid_control.csv")
    run = _run(client, api_headers)
    assert (run["status"], run["case_id"]) == ("pass", None)
    overview = client.get("/", headers=api_headers).json()
    assert overview["cases"] == []


def test_case_page_tracks_current_resolution_state(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    first = _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    url = f"/cases/{failed['case_id']}"
    failing = client.get(url)
    assert "Failed check" in failing.text
    assert "ingest a corrected CSV" in failing.text
    assert "Resolve case</button>" not in failing.text

    second = _ingest(client, api_headers, "corrected_amount.csv")
    _run(client, api_headers)
    case = client.get(url, headers=api_headers).json()
    assert case["submission_id"] == first["submission_id"]
    assert case["checked_submission_id"] == second["submission_id"]
    assert case["resolution"] == {"allowed": True, "reason": "ok"}
    passing = client.get(url)
    assert "Passed check" in passing.text
    assert "press Resolve case to close it" in passing.text
    assert f"source submission {first['submission_id']}, version 1" in passing.text
    assert f"checked submission {second['submission_id']}" in passing.text
    assert "Resolve case</button>" in passing.text

    _ingest(client, api_headers, "valid_control.csv")
    displaced = client.get(url)
    assert "re-upload its latest CSV" in displaced.text
    assert "Resolve case</button>" not in displaced.text
    refused = client.post(f"{url}/resolve")
    assert "Resolve refused (current-missing)" in refused.text
    assert "re-upload its latest CSV" in refused.text

    _ingest(client, api_headers, "corrected_amount.csv")
    _run(client, api_headers)
    resolved = client.post(f"{url}/resolve")
    assert "none, this case is resolved" in resolved.text
    assert "Resolve case</button>" not in resolved.text


def test_browser_action_errors_are_html_and_keep_status(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    for method, url, status, detail in [
        ("post", "/runs", 409, "no submission staged; ingest a CSV first"),
        ("get", "/cases/missing", 404, "case missing not found"),
        ("post", "/cases/missing/resolve", 404, "case missing not found"),
    ]:
        page = client.request(method, url)
        assert page.status_code == status
        assert page.headers["content-type"].startswith("text/html")
        assert detail in page.text
        assert 'href="/"' in page.text
        api = client.request(method, url, headers=api_headers)
        assert api.status_code == status
        assert api.json() == {"detail": detail}


def test_case_page_shows_core_samples_and_complete_historical_rows(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    url = f"/cases/{failed['case_id']}"
    failing = client.get(url)
    assert "Core failure samples" in failing.text
    sample = failing.text.split("Core failure samples", 1)[1].split("</table>", 1)[0]
    for value in ("INV-001", "SUP-42", "2026-09-01", "EUR", "missing"):
        assert value in sample

    corrected = (
        b"invoice_id,supplier_id,invoice_date,currency,amount\n"
        b"INV-001,SUP-NEW,2026-09-02,USD,123.45\n"
    )
    response = client.post(
        "/ingest",
        files={"file": ("correction.csv", corrected, "text/csv")},
        headers=api_headers,
    )
    assert response.status_code == 200
    _run(client, api_headers)
    passing = client.get(url)
    history = passing.text.split("Submission history", 1)[1].split("</table>", 1)[0]
    for value in (
        "SUP-42",
        "2026-09-01",
        "EUR",
        "missing",
        "SUP-NEW",
        "2026-09-02",
        "USD",
        "12345",
    ):
        assert value in history
