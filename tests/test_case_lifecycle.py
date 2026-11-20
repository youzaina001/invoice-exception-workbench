"""Case lifecycle, resolution gate, and Demo Attempt read-only behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy.event as sa_event
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from invoice_triage import cases
from invoice_triage.intake import ingest_submission
from invoice_triage.request_scope import (
    AttemptReadOnlyError,
    install_scope,
    resolve_scope,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _scoped_engine(dsn: str) -> Engine:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    install_scope(engine)
    return engine


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


def test_cases_resolution_gate_covers_all_ten_refusal_reasons_and_ok(
    pg_dsns: dict[str, str],
) -> None:
    intake = _scoped_engine(pg_dsns["intake"])
    audit = _scoped_engine(pg_dsns["audit"])
    admin = make_engine(pg_dsns["admin"])
    try:
        ingest_submission(
            intake,
            filename="missing_amount.csv",
            data=(FIXTURES / "missing_amount.csv").read_bytes(),
        )
        failed = cases.run_current_checks(intake=intake, read_dsn=pg_dsns["read"])
        case_id = str(failed["case_id"])

        detail = cases.get_case_detail(intake=intake, audit=audit, case_id=case_id)
        assert detail["resolution"] == {"allowed": False, "reason": "verdict-not-pass"}
        assert "ingest a corrected CSV" in str(detail["guidance"])
        outcome = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (outcome["resolved"], outcome["reason"]) == (False, "verdict-not-pass")
        assert "ingest a corrected CSV" in str(outcome["guidance"])

        ingest_submission(
            intake,
            filename="corrected_amount.csv",
            data=(FIXTURES / "corrected_amount.csv").read_bytes(),
        )
        stale_detail = cases.get_case_detail(
            intake=intake, audit=audit, case_id=case_id
        )
        assert stale_detail["resolution"] == {
            "allowed": False,
            "reason": "stale-version",
        }
        assert "current version first" in str(stale_detail["guidance"])
        assert (
            cases.resolve_case(intake=intake, audit=audit, case_id=case_id)["reason"]
            == "stale-version"
        )

        cases.run_current_checks(intake=intake, read_dsn=pg_dsns["read"])
        ok_detail = cases.get_case_detail(
            intake=intake,
            audit=audit,
            case_id=case_id,
            refused_reason="current-missing",
        )
        assert ok_detail["resolution"] == {"allowed": True, "reason": "ok"}
        assert "press Resolve case to close it" in str(ok_detail["guidance"])
        refusal = ok_detail["refusal"]
        assert isinstance(refusal, dict)
        assert refusal["reason"] == "current-missing"
        assert "re-upload its latest CSV" in str(refusal["guidance"])

        unknown_refusal = cases.get_case_detail(
            intake=intake,
            audit=audit,
            case_id=case_id,
            refused_reason="unknown-code",
        )["refusal"]
        assert unknown_refusal == {
            "reason": "unknown-code",
            "guidance": "Resolve was refused; run checks and try again.",
        }

        ingest_submission(
            intake,
            filename="valid_control.csv",
            data=(FIXTURES / "valid_control.csv").read_bytes(),
        )
        assert cases.get_case_detail(intake=intake, audit=audit, case_id=case_id)[
            "resolution"
        ] == {"allowed": False, "reason": "current-missing"}
        assert (
            cases.resolve_case(intake=intake, audit=audit, case_id=case_id)["reason"]
            == "current-missing"
        )

        ingest_submission(
            intake,
            filename="corrected_amount.csv",
            data=(FIXTURES / "corrected_amount.csv").read_bytes(),
        )
        with admin.begin() as conn:
            orig_snap = conn.execute(
                text("SELECT snapshot FROM wb.staging_invoice")
            ).scalar_one()
            conn.execute(text("UPDATE wb.staging_invoice SET snapshot = 'tampered'"))
        snap_outcome = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (snap_outcome["resolved"], snap_outcome["reason"]) == (
            False,
            "snapshot-mismatch",
        )
        assert "staged row changed" in str(snap_outcome["guidance"])

        with admin.begin() as conn:
            conn.execute(
                text("UPDATE wb.staging_invoice SET snapshot = :snap"),
                {"snap": orig_snap},
            )
            orig_check = conn.execute(
                text(
                    "SELECT check_id FROM wb.case_verdicts WHERE case_id = :id"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"id": case_id},
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE wb.case_verdicts SET check_id = 'other.check'"
                    " WHERE case_id = :id"
                ),
                {"id": case_id},
            )
        check_outcome = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (check_outcome["resolved"], check_outcome["reason"]) == (
            False,
            "check-mismatch",
        )
        assert "different check" in str(check_outcome["guidance"])

        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE wb.case_verdicts SET check_id = :check WHERE case_id = :id"
                ),
                {"check": orig_check, "id": case_id},
            )
            conn.execute(text("DELETE FROM dq.traces"))
        incomplete = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (incomplete["resolved"], incomplete["reason"]) == (
            False,
            "incomplete-report",
        )
        assert "audit trace is unavailable" in str(incomplete["guidance"])

        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE wb.case_verdicts SET status = 'error' WHERE case_id = :id"
                ),
                {"id": case_id},
            )
        errored = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (errored["resolved"], errored["reason"]) == (False, "errored-report")
        assert "required check errored" in str(errored["guidance"])

        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM wb.case_verdicts WHERE case_id = :id"),
                {"id": case_id},
            )
        no_verdict = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (no_verdict["resolved"], no_verdict["reason"]) == (False, "no-verdict")
        assert "No check run is recorded" in str(no_verdict["guidance"])

        cases.run_current_checks(intake=intake, read_dsn=pg_dsns["read"])
        resolved = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (resolved["resolved"], resolved["reason"]) == (True, "ok")
        assert "press Resolve case to close it" in str(resolved["guidance"])

        closed_detail = cases.get_case_detail(
            intake=intake, audit=audit, case_id=case_id
        )
        assert closed_detail["resolution"] == {
            "allowed": False,
            "reason": "case-not-open",
        }
        assert closed_detail["guidance"] == "none, this case is resolved."
        again = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        assert (again["resolved"], again["reason"]) == (False, "case-not-open")
        assert again["guidance"] == "none, this case is resolved."
    finally:
        intake.dispose()
        audit.dispose()
        admin.dispose()


def test_historical_demo_attempt_distinguishes_open_and_resolved_cases(
    pg_dsns: dict[str, str],
) -> None:
    intake = _scoped_engine(pg_dsns["intake"])
    audit = _scoped_engine(pg_dsns["audit"])
    try:
        attempt1 = resolve_scope(intake, hosted=True, reviewer_id="case-hist-rev")
        ingest_submission(
            intake,
            filename="missing_amount.csv",
            data=(FIXTURES / "missing_amount.csv").read_bytes(),
            scope=attempt1,
        )
        closed_run = cases.run_current_checks(
            intake=intake, read_dsn=pg_dsns["read"], scope=attempt1
        )
        closed_case_id = str(closed_run["case_id"])
        ingest_submission(
            intake,
            filename="corrected_amount.csv",
            data=(FIXTURES / "corrected_amount.csv").read_bytes(),
            scope=attempt1,
        )
        cases.run_current_checks(
            intake=intake, read_dsn=pg_dsns["read"], scope=attempt1
        )
        assert (
            cases.resolve_case(
                intake=intake, audit=audit, case_id=closed_case_id, scope=attempt1
            )["resolved"]
            is True
        )

        ingest_submission(
            intake,
            filename="open_case.csv",
            data=(
                b"invoice_id,supplier_id,invoice_date,currency,amount\n"
                b"INV-OPEN,SUP-42,2026-09-01,EUR,\n"
            ),
            scope=attempt1,
        )
        open_run = cases.run_current_checks(
            intake=intake, read_dsn=pg_dsns["read"], scope=attempt1
        )
        open_case_id = str(open_run["case_id"])

        attempt2 = resolve_scope(
            intake, hosted=True, reviewer_id="case-hist-rev", start_fresh=True
        )
        closed_view = cases.get_case_detail(
            intake=intake, audit=audit, case_id=closed_case_id, scope=attempt2
        )
        assert closed_view["attempt_read_only"] is True
        assert closed_view["resolution"] == {
            "allowed": False,
            "reason": "case-not-open",
        }
        assert closed_view["guidance"] == "none, this case is resolved."

        open_view = cases.get_case_detail(
            intake=intake, audit=audit, case_id=open_case_id, scope=attempt2
        )
        assert open_view["attempt_read_only"] is True
        assert open_view["resolution"] == {
            "allowed": False,
            "reason": "attempt-read-only",
        }
        assert "read-only" in str(open_view["guidance"]).lower()
        assert "run checks and try again" not in str(open_view["guidance"]).lower()

        for case_id in (closed_case_id, open_case_id):
            with pytest.raises(AttemptReadOnlyError, match="attempt-read-only"):
                cases.resolve_case(
                    intake=intake, audit=audit, case_id=case_id, scope=attempt2
                )
    finally:
        intake.dispose()
        audit.dispose()


def test_refused_after_race_rechecks_live_audit_completeness(
    pg_dsns: dict[str, str],
) -> None:
    intake = _scoped_engine(pg_dsns["intake"])
    audit = _scoped_engine(pg_dsns["audit"])
    admin = make_engine(pg_dsns["admin"])
    try:
        ingest_submission(
            intake,
            filename="missing_amount.csv",
            data=(FIXTURES / "missing_amount.csv").read_bytes(),
        )
        failed = cases.run_current_checks(intake=intake, read_dsn=pg_dsns["read"])
        case_id = str(failed["case_id"])
        ingest_submission(
            intake,
            filename="corrected_amount.csv",
            data=(FIXTURES / "corrected_amount.csv").read_bytes(),
        )
        cases.run_current_checks(intake=intake, read_dsn=pg_dsns["read"])

        raced = {"done": False}

        def _lose_race(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            if not raced["done"] and "UPDATE wb.cases SET status" in statement:
                raced["done"] = True
                with admin.begin() as admin_conn:
                    admin_conn.execute(
                        text("UPDATE wb.staging_invoice SET version = 3")
                    )
                    admin_conn.execute(text("DELETE FROM dq.traces"))

        sa_event.listen(intake, "before_cursor_execute", _lose_race)
        try:
            outcome = cases.resolve_case(intake=intake, audit=audit, case_id=case_id)
        finally:
            sa_event.remove(intake, "before_cursor_execute", _lose_race)
        assert raced["done"] is True
        assert (outcome["resolved"], outcome["reason"]) == (
            False,
            "incomplete-report",
        )
        assert "audit trace is unavailable" in str(outcome["guidance"])
    finally:
        intake.dispose()
        audit.dispose()
        admin.dispose()
