"""M1 operator journey: missing amount creates one unresolved case."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_missing_amount_creates_one_unresolved_case(
    client: TestClient, api_headers: dict[str, str]
) -> None:
    csv_bytes = (FIXTURES / "missing_amount.csv").read_bytes()
    response = client.post(
        "/ingest",
        files={"file": ("missing_amount.csv", csv_bytes, "text/csv")},
        headers=api_headers,
    )
    assert response.status_code == 200
    submission = response.json()
    assert submission["duplicate"] is False

    response = client.post("/runs", headers=api_headers)
    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "fail"
    assert run["check_id"] == "staging_invoice.amount_minor.completeness"

    response = client.get(f"/cases/{run['case_id']}", headers=api_headers)
    assert response.status_code == 200
    case = response.json()
    assert case["status"] == "open"
    assert case["submission_id"] == submission["submission_id"]
    assert case["check_id"] == "staging_invoice.amount_minor.completeness"
    assert case["run_id"] == run["run_id"]
    assert case["row"]["amount_minor"] is None
    assert case["core"]["n_failed"] == 1
    assert case["core"]["n_total"] == 1
    assert case["core"]["message"] == "1/1 null amount_minor on staging_invoice"
    samples = case["core"]["sample_failures"]
    assert len(samples) == 1
    assert samples[0]["submission_id"] == submission["submission_id"]
    assert samples[0]["invoice_id"] == "INV-001"
    assert samples[0]["supplier_id"] == "SUP-42"
    assert samples[0]["invoice_date"] == "2026-09-01"
    assert samples[0]["currency"] == "EUR"
    assert samples[0]["amount_minor"] is None
