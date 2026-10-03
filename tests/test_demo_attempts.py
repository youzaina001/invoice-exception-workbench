"""Resume, Start fresh, and the five-attempt cap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import signed_in_reviewer
from fastapi.testclient import TestClient

from ledgerlens.app import create_app
from ledgerlens.settings import Settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture()
def hosted_client(pg_dsns: dict[str, str]) -> TestClient:
    return TestClient(_hosted_app(pg_dsns))


def _hosted_app(pg_dsns: dict[str, str]) -> Any:
    return create_app(
        Settings(
            intake_dsn=pg_dsns["intake"],
            read_dsn=pg_dsns["read"],
            audit_dsn=pg_dsns["audit"],
            hosted=True,
        ),
        authenticate=signed_in_reviewer,
    )


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


def test_returning_reviewer_resumes_the_same_attempt(
    hosted_client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-resume"}
    opened = hosted_client.get("/", headers=headers).json()
    submitted = _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    case_id = str(failed["case_id"])
    case = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert case["history"]["audit"]
    assert case["history"]["verdicts"]

    returned = TestClient(_hosted_app(pg_dsns))
    overview = returned.get("/", headers=headers).json()
    assert overview["workspace_id"] == opened["workspace_id"]
    assert overview["attempt_id"] == opened["attempt_id"]
    assert [row["id"] for row in overview["submissions"]] == [
        submitted["submission_id"]
    ]
    assert [row["id"] for row in overview["cases"]] == [case_id]
    again = returned.get(f"/cases/{case_id}", headers=headers).json()
    assert again["run_id"] == case["run_id"]
    assert again["audit"] == case["audit"]
    assert again["history"]["verdicts"] == case["history"]["verdicts"]
    assert again["history"]["audit"] == case["history"]["audit"]
    resumed = returned.get("/", headers=headers).json()
    assert resumed["attempt_id"] == opened["attempt_id"]


def test_start_fresh_opens_an_empty_current_attempt(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-fresh"}
    opened = hosted_client.get("/", headers=headers).json()
    submitted = _ingest(hosted_client, headers, "missing_amount.csv")
    _run(hosted_client, headers)

    started = hosted_client.post("/demo-attempts/start-fresh", headers=headers)
    assert started.status_code == 200
    body = started.json()
    assert body["workspace_id"] == opened["workspace_id"]
    assert body["previous_attempt_id"] == opened["attempt_id"]
    assert body["attempt_id"] not in {opened["attempt_id"], ""}
    assert isinstance(body["attempt_id"], str)

    current = hosted_client.get("/", headers=headers).json()
    assert current["workspace_id"] == opened["workspace_id"]
    assert current["attempt_id"] == body["attempt_id"]
    assert current["submissions"] == []
    assert current["cases"] == []
    current_page = hosted_client.get("/", headers=headers).text
    assert submitted["submission_id"] not in current_page
    again = hosted_client.get("/", headers=headers).json()
    assert again["attempt_id"] == body["attempt_id"]


def test_previous_attempt_stays_readable_only_to_its_reviewer(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-history"}
    other = {**api_headers, "X-Reviewer": "reviewer-outsider"}
    submitted = _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    case_id = str(failed["case_id"])
    run_id = str(failed["run_id"])
    case = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert case["history"]["audit"]
    started = hosted_client.post("/demo-attempts/start-fresh", headers=headers).json()
    previous = str(started["previous_attempt_id"])

    readable = hosted_client.get(f"/demo-attempts/{previous}", headers=headers)
    assert readable.status_code == 200
    body = readable.json()
    assert body["attempt_id"] == previous
    assert body["workspace_id"] == started["workspace_id"]
    assert [row["id"] for row in body["submissions"]] == [submitted["submission_id"]]
    assert [row["id"] for row in body["cases"]] == [case_id]
    assert any(row["run_id"] == run_id for row in body["check_results"])
    assert any(row["run_id"] == run_id for row in body["audit"])
    assert body["audit"][0]["event_id"]

    hidden = hosted_client.get(f"/demo-attempts/{previous}", headers=other)
    assert hidden.status_code == 404
    assert submitted["submission_id"] not in hidden.text
    assert case_id not in hidden.text
    assert run_id not in hidden.text
    assert "INV-001" not in hidden.text
    assert "SUP-42" not in hidden.text


def test_previous_attempt_refuses_ingest_and_resolve(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-readonly"}
    _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    case_id = str(failed["case_id"])
    _ingest(hosted_client, headers, "corrected_amount.csv")
    passed = _run(hosted_client, headers)
    assert passed["case_id"] == case_id
    started = hosted_client.post("/demo-attempts/start-fresh", headers=headers).json()
    previous = str(started["previous_attempt_id"])
    before = hosted_client.get(f"/demo-attempts/{previous}", headers=headers).json()
    assert before["cases"][0]["status"] == "open"
    historical_page = hosted_client.get(
        f"/cases/{case_id}", headers={"X-Reviewer": "reviewer-readonly"}
    )
    assert "Resolve case</button>" not in historical_page.text
    assert "read-only" in historical_page.text

    pinned = {**headers, "X-Demo-Attempt": previous}

    refused_ingest = hosted_client.post(
        "/ingest",
        files={
            "file": (
                "missing_amount.csv",
                (FIXTURES / "missing_amount.csv").read_bytes(),
                "text/csv",
            )
        },
        headers=pinned,
    )
    assert refused_ingest.status_code == 409
    assert refused_ingest.json()["reason"] == "attempt-read-only"
    assert hosted_client.get("/", headers=headers).json()["submissions"] == []

    refused_resolve = hosted_client.post(f"/cases/{case_id}/resolve", headers=pinned)
    assert refused_resolve.status_code == 409
    assert refused_resolve.json()["reason"] == "attempt-read-only"

    bare = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers)
    assert bare.status_code == 409
    assert bare.json()["reason"] == "attempt-read-only"
    assert (
        hosted_client.get(f"/demo-attempts/{previous}", headers=headers).json()
        == before
    )

    current_submission = _ingest(hosted_client, headers, "missing_amount.csv")
    current_run = _run(hosted_client, headers)
    overview = hosted_client.get("/", headers=headers).json()
    assert overview["attempt_id"] == started["attempt_id"]
    assert [row["id"] for row in overview["submissions"]] == [
        current_submission["submission_id"]
    ]
    assert [row["id"] for row in overview["cases"]] == [current_run["case_id"]]
    assert current_run["case_id"] != case_id
    detail = hosted_client.get(
        f"/cases/{current_run['case_id']}", headers=headers
    ).json()
    assert detail["core"]["n_total"] == 1
    assert (
        hosted_client.get(f"/demo-attempts/{previous}", headers=headers).json()
        == before
    )


def test_sixth_start_fresh_drops_the_oldest_attempt_only(
    hosted_client: TestClient, api_headers: dict[str, str], pg_dsns: dict[str, str]
) -> None:
    other_headers = {**api_headers, "X-Reviewer": "reviewer-cap-other"}
    other_opened = hosted_client.get("/", headers=other_headers).json()
    other_submitted = _ingest(hosted_client, other_headers, "missing_amount.csv")
    other_failed = _run(hosted_client, other_headers)
    other_attempt = hosted_client.get(
        f"/demo-attempts/{other_opened['attempt_id']}", headers=other_headers
    ).json()
    assert other_attempt["audit"]
    other_overview = hosted_client.get("/", headers=other_headers).json()

    headers = {**api_headers, "X-Reviewer": "reviewer-cap"}
    opened = hosted_client.get("/", headers=headers).json()
    submitted = _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    oldest = str(opened["attempt_id"])
    case_id = str(failed["case_id"])
    kept = [oldest]
    for _ in range(4):
        started = hosted_client.post("/demo-attempts/start-fresh", headers=headers)
        assert started.status_code == 200
        kept.append(str(started.json()["attempt_id"]))
    assert len(kept) == 5
    for attempt_id in kept:
        readable = hosted_client.get(f"/demo-attempts/{attempt_id}", headers=headers)
        assert readable.status_code == 200

    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    owner = make_engine(pg_dsns["admin"])
    tables = (
        "wb.submissions",
        "wb.submission_rows",
        "wb.staging_invoice",
        "wb.cases",
        "wb.case_verdicts",
        "dq.check_runs",
        "dq.traces",
    )

    def owner_counts(attempt: str) -> dict[str, int]:
        with owner.connect() as conn:
            return {
                table: conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE attempt_id = :id"),
                    {"id": attempt},
                ).scalar_one()
                for table in tables
            }

    # Owner can inspect both a historical attempt and another Reviewer's rows.
    historical_counts = owner_counts(oldest)
    other_counts = owner_counts(other_opened["attempt_id"])
    assert all(count > 0 for count in historical_counts.values())
    assert all(count > 0 for count in other_counts.values())

    capped = hosted_client.post("/demo-attempts/start-fresh", headers=headers)
    assert capped.status_code == 200
    body = capped.json()
    assert body["workspace_id"] == opened["workspace_id"]
    assert body["previous_attempt_id"] == kept[-1]
    assert body["attempt_id"] not in kept
    surviving = [*kept[1:], str(body["attempt_id"])]
    assert len(surviving) == 5
    for attempt_id in surviving:
        readable = hosted_client.get(f"/demo-attempts/{attempt_id}", headers=headers)
        assert readable.status_code == 200
    gone = hosted_client.get(f"/demo-attempts/{oldest}", headers=headers)
    assert gone.status_code == 404
    missing_case = hosted_client.get(f"/cases/{case_id}", headers=headers)
    assert missing_case.status_code == 404
    assert submitted["submission_id"] not in missing_case.text
    current = hosted_client.get("/", headers=headers).json()
    assert current["attempt_id"] == body["attempt_id"]
    assert current["workspace_id"] == opened["workspace_id"]
    assert current["submissions"] == []

    assert hosted_client.get("/", headers=other_headers).json() == other_overview
    assert (
        hosted_client.get(
            f"/demo-attempts/{other_opened['attempt_id']}", headers=other_headers
        ).json()
        == other_attempt
    )
    assert owner_counts(oldest) == dict.fromkeys(tables, 0)
    assert owner_counts(other_opened["attempt_id"]) == other_counts
    owner.dispose()
    assert other_submitted["submission_id"] in {
        row["id"] for row in other_attempt["submissions"]
    }
    assert other_failed["case_id"] in {row["id"] for row in other_attempt["cases"]}


def test_previous_attempt_history_and_checks_stay_put(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-history-scope"}
    _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    case_id = str(failed["case_id"])
    before = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    history_ids = [row["submission_id"] for row in before["history"]["submissions"]]
    assert history_ids

    started = hosted_client.post("/demo-attempts/start-fresh", headers=headers).json()
    previous = str(started["previous_attempt_id"])
    pinned = {**headers, "X-Demo-Attempt": previous}
    refused_run = hosted_client.post("/runs", headers=pinned)
    assert refused_run.status_code == 409
    assert refused_run.json()["reason"] == "attempt-read-only"

    later = _ingest(hosted_client, headers, "corrected_amount.csv")
    after = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert [
        row["submission_id"] for row in after["history"]["submissions"]
    ] == history_ids
    assert later["submission_id"] not in history_ids
    assert after["history"]["audit"] == before["history"]["audit"]


def test_retention_cannot_delete_current_retained_or_other_workspace_audit(
    hosted_client: TestClient, pg_dsns: dict[str, str], api_headers: dict[str, str]
) -> None:
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    headers = {**api_headers, "X-Reviewer": "retention-owner"}
    first = hosted_client.get("/", headers=headers).json()["attempt_id"]
    _ingest(hosted_client, headers, "missing_amount.csv")
    _run(hosted_client, headers)
    current = hosted_client.post("/demo-attempts/start-fresh", headers=headers).json()[
        "attempt_id"
    ]
    other = hosted_client.get(
        "/", headers={**api_headers, "X-Reviewer": "other"}
    ).json()["attempt_id"]
    engine = make_engine(pg_dsns["intake"])
    for attempt in (first, current, other):
        with pytest.raises(DBAPIError, match="retention not permitted"):
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.hosted', 'true', true)"))
                conn.execute(
                    text("SELECT set_config('app.reviewer', 'retention-owner', true)")
                )
                conn.execute(
                    text("SELECT wb.delete_attempt_audit(:id)"), {"id": attempt}
                )
    assert hosted_client.get("/demo-attempts/" + first, headers=headers).json()["audit"]
    engine.dispose()


def test_hosted_browser_has_start_fresh_and_read_only_history(
    hosted_client: TestClient,
) -> None:
    headers = {"X-Reviewer": "browser-reviewer"}
    api = {**headers, "Accept": "application/json"}
    _ingest(hosted_client, api, "missing_amount.csv")
    _run(hosted_client, api)
    page = hosted_client.get("/", headers=headers)
    assert page.status_code == 200
    assert 'action="/demo-attempts/start-fresh"' in page.text
    previous = hosted_client.post("/demo-attempts/start-fresh", headers=api).json()[
        "previous_attempt_id"
    ]
    page = hosted_client.get("/", headers=headers)
    assert f'href="/demo-attempts/{previous}"' in page.text
    history = hosted_client.get(f"/demo-attempts/{previous}", headers=headers)
    assert history.status_code == 200
    assert "Read-only Demo Attempt" in history.text
    assert "missing_amount.csv" in history.text
    assert 'action="/ingest"' not in history.text
    assert 'action="/runs"' not in history.text
