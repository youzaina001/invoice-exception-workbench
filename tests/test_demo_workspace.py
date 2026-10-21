"""Private Demo Workspace on the hosted configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import signed_in_reviewer
from fastapi.testclient import TestClient

from invoice_triage.app import create_app
from invoice_triage.settings import Settings

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_REGISTRATION_WORDS = ("register", "sign-up", "signup", "invite")


@pytest.fixture()
def hosted_client(pg_dsns: dict[str, str]) -> TestClient:
    settings = Settings(
        intake_dsn=pg_dsns["intake"],
        read_dsn=pg_dsns["read"],
        audit_dsn=pg_dsns["audit"],
        hosted=True,
    )
    return TestClient(create_app(settings, authenticate=signed_in_reviewer))


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


def test_unsigned_hosted_request_returns_no_journey_and_no_registration(
    client: TestClient,
    hosted_client: TestClient,
    api_headers: dict[str, str],
) -> None:
    ingested = _ingest(client, api_headers, "missing_amount.csv")
    failed = _run(client, api_headers)
    case_id = str(failed["case_id"])
    upload = {
        "file": (
            "missing_amount.csv",
            (FIXTURES / "missing_amount.csv").read_bytes(),
            "text/csv",
        )
    }
    requests: list[tuple[str, str, dict[str, Any]]] = [
        ("get", "/", {}),
        ("post", "/ingest", {"files": upload}),
        ("post", "/runs", {}),
        ("get", f"/cases/{case_id}", {}),
        ("post", f"/cases/{case_id}/resolve", {}),
        ("get", "/demo-attempts/unsigned-attempt", {}),
        ("post", "/demo-attempts/start-fresh", {}),
    ]
    unsigned_headers = (
        api_headers,
        {**api_headers, "X-Reviewer": ""},
        {**api_headers, "X-Reviewer": " "},
    )
    for header in unsigned_headers:
        for method, url, extra in requests:
            response = hosted_client.request(method, url, headers=header, **extra)
            assert response.status_code == 401
            assert response.headers["content-type"].startswith("application/json")
            body = response.json()
            for key in ("submissions", "cases", "checks", "check_results", "audit"):
                assert body.get(key) in (None, [])
            lowered = response.text.lower()
            assert not any(word in lowered for word in _REGISTRATION_WORDS)
            assert ingested["submission_id"] not in response.text
            assert case_id not in response.text
            assert "INV-001" not in response.text
            assert "12345" not in response.text
    still = client.get(f"/cases/{case_id}", headers=api_headers).json()
    assert still["status"] == "open"


def test_first_signed_in_reviewer_receives_empty_demo_attempt(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "reviewer-one"}
    first = hosted_client.get("/", headers=headers)
    assert first.status_code == 200
    body = first.json()
    assert body["submissions"] == []
    assert body["cases"] == []
    workspace_id = body["workspace_id"]
    attempt_id = body["attempt_id"]
    assert isinstance(workspace_id, str) and workspace_id
    assert isinstance(attempt_id, str) and attempt_id
    assert workspace_id != attempt_id
    again = hosted_client.get("/", headers=headers)
    assert again.status_code == 200
    resumed = again.json()
    assert resumed["workspace_id"] == workspace_id
    assert resumed["attempt_id"] == attempt_id
    assert resumed["submissions"] == []
    assert resumed["cases"] == []


def test_intake_cannot_insert_an_explicit_foreign_reviewer(
    hosted_client: TestClient, pg_dsns: dict[str, str], api_headers: dict[str, str]
) -> None:
    from airflow_dq_agent.warehouse.db import make_engine
    from psycopg.errors import InsufficientPrivilege
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    victim_headers = {**api_headers, "X-Reviewer": "victim"}
    victim = hosted_client.get("/", headers=victim_headers).json()
    attacker = hosted_client.get(
        "/", headers={**api_headers, "X-Reviewer": "attacker"}
    ).json()
    engine = make_engine(pg_dsns["intake"])
    try:
        with pytest.raises(DBAPIError) as denied, engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.hosted', 'true', true)"))
            conn.execute(text("SELECT set_config('app.reviewer', 'attacker', true)"))
            conn.execute(
                text("SELECT set_config('app.attempt', :id, true)"),
                {"id": attacker["attempt_id"]},
            )
            conn.execute(
                text(
                    "INSERT INTO wb.submissions "
                    "(id, invoice_ref, version, content_hash, filename, "
                    "reviewer_id, attempt_id) VALUES "
                    "('foreign-insert', 'INV-FOREIGN', 1, 'hash', "
                    "'synthetic.csv', 'victim', :id)"
                ),
                {"id": victim["attempt_id"]},
            )
        assert isinstance(denied.value.orig, InsufficientPrivilege)
        assert "row-level security" in (denied.value.orig.diag.message_primary or "")
    finally:
        engine.dispose()
    assert hosted_client.get("/", headers=victim_headers).json() == victim


def test_hosted_displaced_invoice_requires_restage_before_resolution(
    hosted_client: TestClient, api_headers: dict[str, str]
) -> None:
    headers = {**api_headers, "X-Reviewer": "multi-invoice-reviewer"}
    opened = hosted_client.get("/", headers=headers).json()
    _ingest(hosted_client, headers, "missing_amount.csv")
    case_id = _run(hosted_client, headers)["case_id"]
    corrected = _ingest(hosted_client, headers, "corrected_amount.csv")
    assert _run(hosted_client, headers)["status"] == "pass"
    _ingest(hosted_client, headers, "valid_control.csv")
    displaced = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers).json()
    assert (displaced["resolved"], displaced["reason"]) == (False, "current-missing")
    page = hosted_client.get(
        f"/cases/{case_id}", headers={"X-Reviewer": "multi-invoice-reviewer"}
    )
    assert "re-upload its latest CSV" in page.text
    assert "Resolve case</button>" not in page.text

    restaged = _ingest(hosted_client, headers, "corrected_amount.csv")
    assert restaged["duplicate"] is True
    assert restaged["made_current"] is True
    assert restaged["submission_id"] == corrected["submission_id"]
    assert restaged["version"] == 2
    checked = _run(hosted_client, headers)
    assert (checked["status"], checked["case_id"], checked["version"]) == (
        "pass",
        case_id,
        2,
    )
    case = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert case["core"]["n_total"] == 1
    resolved = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
    overview = hosted_client.get("/", headers=headers).json()
    assert overview["attempt_id"] == opened["attempt_id"]
    assert len(overview["submissions"]) == 3


def test_reviewer_resolves_uploaded_attempt_through_existing_gate(
    hosted_client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    headers = {**api_headers, "X-Reviewer": "reviewer-one"}
    opened = hosted_client.get("/", headers=headers).json()
    workspace_id = opened["workspace_id"]
    attempt_id = opened["attempt_id"]

    first = _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    assert failed["status"] == "fail"
    case_id = str(failed["case_id"])
    refused = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers).json()
    assert (refused["resolved"], refused["reason"]) == (False, "verdict-not-pass")

    second = _ingest(hosted_client, headers, "corrected_amount.csv")
    assert (second["version"], second["duplicate"]) == (2, False)
    passed = _run(hosted_client, headers)
    assert passed["status"] == "pass"
    assert (passed["case_id"], passed["case_status"]) == (case_id, "open")

    case = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert case["status"] == "open"
    assert case["submission_id"] == first["submission_id"]
    amounts = [item["row"]["amount_minor"] for item in case["history"]["submissions"]]
    assert amounts == [None, 12345]
    assert case["resolution"] == {"allowed": True, "reason": "ok"}

    resolved = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
    assert resolved["resolved_run_id"] == passed["run_id"]
    closed = hosted_client.get(f"/cases/{case_id}", headers=headers).json()
    assert closed["status"] == "resolved"
    again = hosted_client.post(f"/cases/{case_id}/resolve", headers=headers).json()
    assert (again["resolved"], again["reason"]) == (False, "case-not-open")

    overview = hosted_client.get("/", headers=headers).json()
    assert overview["workspace_id"] == workspace_id
    assert overview["attempt_id"] == attempt_id
    assert [row["id"] for row in overview["submissions"]] == [
        second["submission_id"],
        first["submission_id"],
    ]
    assert [row["id"] for row in overview["cases"]] == [case_id]

    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        staged = conn.execute(text("SELECT COUNT(*) FROM wb.staging_invoice")).scalar()
    assert staged == 1


def test_second_reviewer_cannot_read_or_change_first_workspace(
    hosted_client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    first_headers = {**api_headers, "X-Reviewer": "reviewer-one"}
    second_headers = {**api_headers, "X-Reviewer": "reviewer-two"}
    opened = hosted_client.get("/", headers=first_headers).json()
    first = _ingest(hosted_client, first_headers, "missing_amount.csv")
    failed = _run(hosted_client, first_headers)
    case_id = str(failed["case_id"])
    run_id = str(failed["run_id"])

    second = hosted_client.get("/", headers=second_headers)
    assert second.status_code == 200
    isolated = second.json()
    assert isolated["submissions"] == []
    assert isolated["cases"] == []
    assert isolated["workspace_id"] != opened["workspace_id"]
    assert isolated["attempt_id"] != opened["attempt_id"]
    assert first["submission_id"] not in second.text
    assert case_id not in second.text
    assert run_id not in second.text
    assert "INV-001" not in second.text

    peeked = hosted_client.get(f"/cases/{case_id}", headers=second_headers)
    assert peeked.status_code == 404
    assert first["submission_id"] not in peeked.text
    assert run_id not in peeked.text
    assert "INV-001" not in peeked.text
    assert "SUP-42" not in peeked.text
    tampered = hosted_client.post(f"/cases/{case_id}/resolve", headers=second_headers)
    assert tampered.status_code == 404
    assert "INV-001" not in tampered.text
    untouched = hosted_client.get(f"/cases/{case_id}", headers=first_headers).json()
    assert untouched["status"] == "open"
    assert untouched["submission_id"] == first["submission_id"]

    own = _ingest(hosted_client, second_headers, "missing_amount.csv")
    assert own["duplicate"] is False
    assert own["submission_id"] != first["submission_id"]
    own_run = _run(hosted_client, second_headers)
    assert own_run["status"] == "fail"
    assert own_run["case_id"] != case_id
    own_case = hosted_client.get(
        f"/cases/{own_run['case_id']}", headers=second_headers
    ).json()
    assert own_case["core"]["n_total"] == 1
    assert own_case["submission_id"] == own["submission_id"]
    second_overview = hosted_client.get("/", headers=second_headers).json()
    assert [row["id"] for row in second_overview["submissions"]] == [
        own["submission_id"]
    ]
    assert [row["id"] for row in second_overview["cases"]] == [own_run["case_id"]]
    first_overview = hosted_client.get("/", headers=first_headers).json()
    assert [row["id"] for row in first_overview["submissions"]] == [
        first["submission_id"]
    ]
    assert [row["id"] for row in first_overview["cases"]] == [case_id]

    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        staged = conn.execute(text("SELECT COUNT(*) FROM wb.staging_invoice")).scalar()
    assert staged == 2


def _bare_counts(dsn: str, tables: tuple[str, ...]) -> dict[str, int]:
    """Application-role query that sets no Reviewer and no hosted switch."""
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    engine = make_engine(dsn)
    with engine.begin() as conn:
        counts: dict[str, int] = {}
        for table in tables:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            counts[table] = int(count or 0)
    return counts


def _hosted_counts(dsn: str, tables: tuple[str, ...]) -> dict[str, int]:
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    engine = make_engine(dsn)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.hosted', 'true', true)"))
        counts: dict[str, int] = {}
        for table in tables:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            counts[table] = int(count or 0)
    return counts


def test_hosted_query_without_reviewer_sees_nothing_owner_sees_both(
    hosted_client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    for reviewer, filename in (
        ("reviewer-one", "missing_amount.csv"),
        ("reviewer-two", "corrected_amount.csv"),
    ):
        headers = {**api_headers, "X-Reviewer": reviewer}
        _ingest(hosted_client, headers, filename)
        _run(hosted_client, headers)

    tables = (
        "wb.submissions",
        "wb.submission_rows",
        "wb.staging_invoice",
        "wb.cases",
        "wb.case_verdicts",
        "wb.demo_workspaces",
        "wb.demo_attempts",
    )
    assert _bare_counts(pg_dsns["intake"], tables) == dict.fromkeys(tables, 0)
    assert _hosted_counts(pg_dsns["intake"], tables) == dict.fromkeys(tables, 0)
    assert all(count > 0 for count in _bare_counts(pg_dsns["admin"], tables).values())
    audit_hidden = _hosted_counts(pg_dsns["audit"], ("dq.traces",))
    assert audit_hidden == {"dq.traces": 0}
    # The audit login stays append-only for check runs. A temporary select
    # still returns nothing when hosted is set and the Reviewer is not.
    owner_grant = make_engine(pg_dsns["admin"])
    with owner_grant.begin() as conn:
        conn.execute(text("GRANT SELECT ON dq.check_runs TO dq_audit"))
    try:
        hidden_runs = _hosted_counts(pg_dsns["audit"], ("dq.check_runs",))
    finally:
        with owner_grant.begin() as conn:
            conn.execute(text("REVOKE SELECT ON dq.check_runs FROM dq_audit"))
    assert hidden_runs == {"dq.check_runs": 0}

    owner = make_engine(pg_dsns["admin"])
    with owner.connect() as conn:
        reviewers = {
            str(row[0])
            for row in conn.execute(
                text("SELECT reviewer_id FROM wb.demo_workspaces")
            ).all()
        }
        submissions = int(
            conn.execute(text("SELECT COUNT(*) FROM wb.submissions")).scalar() or 0
        )
        traces = int(conn.execute(text("SELECT COUNT(*) FROM dq.traces")).scalar() or 0)
        check_runs = int(
            conn.execute(text("SELECT COUNT(*) FROM dq.check_runs")).scalar() or 0
        )
    assert reviewers == {"reviewer-one", "reviewer-two"}
    assert submissions == 2
    assert traces > 0
    assert check_runs > 0

    role_sql = text(
        "SELECT rolsuper, rolbypassrls, oid = ("
        " SELECT datdba FROM pg_database WHERE datname = current_database()"
        ") FROM pg_roles WHERE rolname = current_user"
    )
    for key in ("intake", "read", "audit"):
        engine = make_engine(pg_dsns[key])
        with engine.connect() as conn:
            superuser, bypass, owner_role = conn.execute(role_sql).one()
        assert (bool(superuser), bool(bypass), bool(owner_role)) == (
            False,
            False,
            False,
        )


_SKIP_PARTS = {
    ".venv",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "fixtures",
}
_RAW_HEADER = "invoice_id,supplier_id,invoice_date,currency,amount"
_SENSITIVE_STRINGS = ("INV-001", "SUP-42", "EUR", "2026-09-01")
_SENSITIVE_INT = 12345


def _repo_files(root: Path) -> set[Path]:
    found: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file() or any(part in _SKIP_PARTS for part in path.parts):
            continue
        found.add(path.relative_to(root))
    return found


def _walk(value: Any) -> Any:
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def test_original_csv_is_not_kept_and_core_audit_has_no_row_values(
    hosted_client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    from decimal import Decimal

    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    root = Path(__file__).resolve().parent.parent
    before = _repo_files(root)
    raw_files = [
        (FIXTURES / name).read_bytes()
        for name in ("missing_amount.csv", "corrected_amount.csv")
    ]
    headers = {**api_headers, "X-Reviewer": "reviewer-one"}
    _ingest(hosted_client, headers, "missing_amount.csv")
    failed = _run(hosted_client, headers)
    _ingest(hosted_client, headers, "corrected_amount.csv")
    passed = _run(hosted_client, headers)
    resolved = hosted_client.post(
        f"/cases/{failed['case_id']}/resolve", headers=headers
    ).json()
    assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
    assert resolved["resolved_run_id"] == passed["run_id"]
    assert _repo_files(root) == before

    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT table_schema, table_name, column_name, data_type"
                " FROM information_schema.columns"
                " WHERE table_schema IN ('wb', 'dq')"
                " AND data_type IN ("
                " 'text', 'character varying', 'json', 'jsonb', 'bytea')"
            )
        ).all()
        stored: list[str] = []
        for schema, table, column, data_type in columns:
            ident = f'"{schema}"."{table}"."{column}"'
            relation = f'"{schema}"."{table}"'
            if data_type == "bytea":
                expression = f"convert_from({ident}, 'UTF8')"
            else:
                expression = f"{ident}::text"
            rows = conn.execute(
                text(f"SELECT {expression} FROM {relation} WHERE {ident} IS NOT NULL")
            ).all()
            stored.extend(str(row[0]) for row in rows if row[0] is not None)
        bodies = [
            row[0] for row in conn.execute(text("SELECT body FROM dq.check_runs")).all()
        ] + [row[0] for row in conn.execute(text("SELECT body FROM dq.traces")).all()]
    blob = "\n".join(stored)
    assert _RAW_HEADER not in blob
    for raw in raw_files:
        assert raw.decode() not in blob
        assert "123.45" not in blob
    assert len(bodies) > 0
    values = [leaf for body in bodies for leaf in _walk(body)]
    texts = [leaf for leaf in values if isinstance(leaf, str)]
    assert not any(any(item in leaf for leaf in texts) for item in _SENSITIVE_STRINGS)
    numbers = [
        Decimal(str(leaf))
        for leaf in values
        if isinstance(leaf, (int, float, Decimal)) and not isinstance(leaf, bool)
    ]
    assert Decimal(_SENSITIVE_INT) not in numbers
    assert Decimal("123.45") not in numbers
    assert not any(leaf in ("12345", "123.45") for leaf in texts)
