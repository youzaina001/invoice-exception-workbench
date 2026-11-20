"""PDF correction acceptance at HTTP/PostgreSQL boundaries; development data only."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_evaluation import make_pdf
from test_pdf_intake import HEADERS, INVOICE, action, overview, upload

DEVELOPMENT = Path(__file__).resolve().parents[1] / "fixtures/extraction/development"


@pytest.mark.parametrize(
    "document,expected",
    [
        ("18e73a9c", "pass"),  # S01: complete
        ("c2a83501", "fail"),  # S02: missing amount
        ("a6f78492", "pass"),  # S03: real zero
        ("3a51d9e0", "fail"),  # S04: conflicting amounts
        ("fd4207ab", "refuse"),  # S05: missing invoice ID
        ("84b6c31d", "refuse"),  # S06: ambiguous invoice ID
        ("0c9e7b63", "refuse"),  # S07: missing supplier ID
        ("b760a42e", "refuse"),  # S08: missing date
        ("ed138c95", "refuse"),  # S09: ambiguous currency
        ("5f0a72d6", "pass"),  # S10: document instructions
        ("9d7f021b", "refuse"),  # ambiguous supplier
        ("624ec9af", "refuse"),  # ambiguous date
        ("71c54e03", "refuse"),  # missing currency
    ],
)
def test_development_pdf_intake_states(
    client: TestClient, document: str, expected: str
) -> None:
    # Start with existing history: refused inputs must leave it intact.
    action(client, action(client, upload(client), "confirm"), "intake")
    before = overview(client)
    source = (DEVELOPMENT / "pdfs" / f"{document}.pdf").read_bytes()
    draft = upload(client, source)
    if expected == "refuse":
        action(client, draft, "confirm", status=400)
        action(client, draft, "intake", status=409)
        assert overview(client) == before
        assert client.get(f"/pdf-drafts/{draft['id']}/source").content == source
        assert client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json() == draft
        return
    action(client, action(client, draft, "confirm"), "intake")
    response = client.post("/runs", headers=HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == expected
    assert (result["case_id"] is not None) == (expected == "fail")
    assert draft["fields"] == draft["raw"]["fields"]
    if expected == "fail":
        detail = client.get(f"/cases/{result['case_id']}", headers=HEADERS).json()
        assert detail["row"]["amount_minor"] is None
        assert detail["amount_state"] == draft["fields"]["amount_minor"]["state"]


@pytest.mark.parametrize("fault", ["missing-audit", "error", "wrong-audit", "snapshot"])
def test_pdf_resolution_refuses_unusable_core_evidence(
    client: TestClient, pg_dsns: dict[str, str], fault: str
) -> None:
    for document in ("c2a83501", "a6f78492"):
        # Both PDFs need the same reference; edit only the reference of the zero PDF.
        draft = upload(client, (DEVELOPMENT / "pdfs" / f"{document}.pdf").read_bytes())
        fields = deepcopy(draft["fields"])
        fields["invoice_id"]["value"] = "PDF-EVIDENCE"
        response = client.post(
            f"/pdf-drafts/{draft['id']}/edit",
            headers=HEADERS,
            json={"revision": draft["revision"], "fields": fields},
        )
        response.raise_for_status()
        action(client, action(client, response.json(), "confirm"), "intake")
        run = client.post("/runs", headers=HEADERS).json()
    assert run["status"] == "pass"
    case_url = f"/cases/{run['case_id']}"
    faults = {
        "missing-audit": ("DELETE FROM dq.traces", "incomplete-report"),
        "error": ("UPDATE wb.case_verdicts SET status = 'error'", "errored-report"),
        "wrong-audit": (
            "UPDATE wb.case_verdicts SET audit_event_id = 'unrelated-event'",
            "incomplete-report",
        ),
        "snapshot": (
            "UPDATE wb.staging_invoice SET snapshot = 'changed-after-checks'",
            "snapshot-mismatch",
        ),
    }
    sql, reason = faults[fault]
    engine = make_engine(pg_dsns["admin"])
    try:
        # Deliberate database fault injection; no provider output or scoring claim.
        with engine.begin() as conn:
            conn.execute(text(sql))
        response = client.post(f"{case_url}/resolve", headers=HEADERS).json()
        assert response["resolved"] is False
        assert response["reason"] == reason
        assert client.get(case_url, headers=HEADERS).json()["status"] == "open"
    finally:
        engine.dispose()


def test_pdf_source_stays_out_of_core_audit_and_jsonl(
    client: TestClient, pg_dsns: dict[str, str]
) -> None:
    marker = "PDF-PRIVATE-SOURCE-TEXT-DO-NOT-AUDIT"
    for content in (INVOICE.replace("Invoice total: 12.34\n", ""), INVOICE):
        draft = upload(client, make_pdf([content + marker + "\n"]))
        action(client, action(client, draft, "confirm"), "intake")
        client.post("/runs", headers=HEADERS).raise_for_status()
    engine = make_engine(pg_dsns["admin"])
    try:
        with engine.connect() as conn:
            bodies = list(conn.execute(text("SELECT body FROM dq.traces")).scalars())
            bodies += list(
                conn.execute(text("SELECT body FROM dq.check_runs")).scalars()
            )
        assert bodies
        payloads = [json.dumps(body) for body in bodies]
        payloads += [
            path.read_text() for path in Path(os.environ["TRACES_DIR"]).glob("*.jsonl")
        ]
        for payload in payloads:
            for private in (marker, "PDF-001", "SUP-001", "Invoice total:", "%PDF-"):
                assert private not in payload
    finally:
        engine.dispose()
