"""M1 slice: run checks, present cases, and resolve exceptions."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping

from invoice_triage.governance import (
    AMOUNT_CHECK_ID,
    execute_amount_check,
    read_audit_lineage,
)
from invoice_triage.intake import current_staged, read_submission_history
from invoice_triage.request_scope import (
    AttemptReadOnlyError,
    ExecutionScope,
    scoped_transaction,
)
from invoice_triage.resolution import (
    ResolutionDecision,
    ResolutionRequest,
    decide_resolution,
)


class NothingStagedError(RuntimeError):
    pass


class StaleStagedError(RuntimeError):
    pass


class CaseNotFoundError(RuntimeError):
    pass


def _attempt_sql(attempt: str | None) -> tuple[str, dict[str, str]]:
    if attempt is None:
        return "", {}
    return " AND attempt_id = :attempt", {"attempt": attempt}


def run_current_checks(
    *,
    intake: Engine,
    read_dsn: str,
    scope: ExecutionScope | None = None,
    _suite_runner: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    resolved = scope or ExecutionScope.local()
    # ponytail: serialize the singleton staging journey; use per-invoice
    # staging and locks if simultaneous invoice checks become necessary.
    with scoped_transaction(intake, resolved, mutate=True, lock_staging=True) as conn:
        staged = current_staged(conn, attempt_id=resolved.attempt_id)
        if staged is None:
            raise NothingStagedError("no submission staged; ingest a CSV first")
        invoice_ref = str(staged["invoice_ref"])
        submission_id = str(staged["submission_id"])
        version = int(staged["version"])
        snapshot = str(staged["snapshot"])
        checked = execute_amount_check(read_dsn, _suite_runner=_suite_runner)
        status = checked.status
        if current_staged(conn, attempt_id=resolved.attempt_id) != staged:
            raise StaleStagedError("staging changed during the check run; rerun checks")
        extra, extra_params = _attempt_sql(resolved.attempt_id)
        case_row = (
            conn.execute(
                text(
                    "SELECT id FROM wb.cases"
                    " WHERE invoice_ref = :ref AND check_id = :check"
                    " AND status = 'open'" + extra
                ),
                {"ref": invoice_ref, "check": AMOUNT_CHECK_ID, **extra_params},
            )
            .mappings()
            .first()
        )
        if case_row is None and status == "pass":
            return {
                "submission_id": submission_id,
                "invoice_ref": invoice_ref,
                "version": version,
                "run_id": checked.run_id,
                "report_id": checked.report_id,
                "check_id": AMOUNT_CHECK_ID,
                "status": status,
                "case_id": None,
                "case_status": None,
            }
        if case_row is None:
            case_id = uuid4().hex
            conn.execute(
                text(
                    "INSERT INTO wb.cases"
                    " (id, invoice_ref, source_submission_id, check_id)"
                    " VALUES (:id, :ref, :sid, :check)"
                ),
                {
                    "id": case_id,
                    "ref": invoice_ref,
                    "sid": submission_id,
                    "check": AMOUNT_CHECK_ID,
                },
            )
        else:
            case_id = str(case_row["id"])
        conn.execute(
            text(
                "INSERT INTO wb.case_verdicts"
                " (case_id, submission_id, version, snapshot, run_id, report_id,"
                " check_id, status, audit_event_id, n_failed, n_total, message,"
                " sample_failures)"
                " VALUES (:cid, :sid, :ver, :snap, :run, :rep, :check,"
                " :status, :evt, :nfail, :ntot, :msg, CAST(:samples AS jsonb))"
            ),
            {
                "cid": case_id,
                "sid": submission_id,
                "ver": version,
                "snap": snapshot,
                "run": checked.run_id,
                "rep": checked.report_id,
                "check": AMOUNT_CHECK_ID,
                "status": status,
                "evt": checked.audit_event_id,
                "nfail": checked.n_failed,
                "ntot": checked.n_total,
                "msg": checked.message,
                "samples": json.dumps(checked.sample_failures),
            },
        )
        return {
            "submission_id": submission_id,
            "invoice_ref": invoice_ref,
            "version": version,
            "run_id": checked.run_id,
            "report_id": checked.report_id,
            "check_id": AMOUNT_CHECK_ID,
            "status": status,
            "case_id": case_id,
            "case_status": "open",
        }


def get_case_detail(
    *,
    intake: Engine,
    audit: Engine,
    case_id: str,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    resolved = scope or ExecutionScope.local()
    with scoped_transaction(intake, resolved, mutate=False) as conn:
        case = (
            conn.execute(
                text(
                    "SELECT id, invoice_ref, source_submission_id, check_id, status,"
                    " created_at, resolved_at, attempt_id FROM wb.cases WHERE id = :id"
                ),
                {"id": case_id},
            )
            .mappings()
            .first()
        )
        if case is None:
            raise CaseNotFoundError(f"case {case_id} not found")
        verdict = (
            conn.execute(
                text(
                    "SELECT submission_id, version, snapshot, check_id, run_id,"
                    " report_id, status, audit_event_id, n_failed, n_total,"
                    " message, sample_failures"
                    " FROM wb.case_verdicts WHERE case_id = :id"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"id": case_id},
            )
            .mappings()
            .first()
        )
        if verdict is None:
            raise CaseNotFoundError(f"case {case_id} has no verdict")
        case_attempt = (
            str(case["attempt_id"]) if case["attempt_id"] is not None else None
        )
        history_submissions = read_submission_history(
            conn, invoice_ref=str(case["invoice_ref"]), attempt_id=case_attempt
        )
        checked_entry = next(
            (
                entry
                for entry in history_submissions
                if entry["submission_id"] == str(verdict["submission_id"])
            ),
            None,
        )
        if checked_entry is None:
            raise CaseNotFoundError(f"case {case_id} evidence missing")
        current = current_staged(
            conn, invoice_ref=str(case["invoice_ref"]), attempt_id=case_attempt
        )
        source_version: int = conn.execute(
            text("SELECT version FROM wb.submissions WHERE id = :id"),
            {"id": str(case["source_submission_id"])},
        ).scalar_one()
        history_verdicts = (
            conn.execute(
                text(
                    "SELECT version, run_id, status, sample_failures"
                    " FROM wb.case_verdicts WHERE case_id = :id ORDER BY id"
                ),
                {"id": case_id},
            )
            .mappings()
            .all()
        )
        audit_view = read_audit_lineage(
            audit, verdicts=history_verdicts, latest_verdict=verdict
        )
    decision = _resolution_decision(case, verdict, current, audit_view.report_available)
    if resolved.attempt_id is not None and case["attempt_id"] != resolved.attempt_id:
        decision = ResolutionDecision(False, "attempt-read-only")
    return {
        "case_id": str(case["id"]),
        "invoice_ref": str(case["invoice_ref"]),
        "submission_id": str(case["source_submission_id"]),
        "source_version": int(source_version),
        "checked_submission_id": str(verdict["submission_id"]),
        "resolution": {"allowed": decision.allowed, "reason": decision.reason},
        "version": int(verdict["version"]),
        "check_id": str(case["check_id"]),
        "status": str(case["status"]),
        "run_id": str(verdict["run_id"]),
        "report_id": str(verdict["report_id"]),
        "verdict": str(verdict["status"]),
        "row": checked_entry["row"],
        "amount_state": checked_entry["amount_state"],
        "core": {
            "n_failed": verdict["n_failed"],
            "n_total": verdict["n_total"],
            "message": verdict["message"],
            "sample_failures": verdict["sample_failures"],
        },
        "audit": audit_view.labels,
        "history": {
            "audit": audit_view.events,
            "submissions": history_submissions,
            "verdicts": [
                {
                    "version": int(v["version"]),
                    "run_id": str(v["run_id"]),
                    "status": str(v["status"]),
                    "sample_failures": v["sample_failures"],
                }
                for v in history_verdicts
            ],
        },
        "created_at": case["created_at"].isoformat(),
        "resolved_at": (
            case["resolved_at"].isoformat() if case["resolved_at"] is not None else None
        ),
    }


def _refused_after_race(
    conn: Connection,
    *,
    case_id: str,
    verdict: RowMapping,
    attempt_id: str | None = None,
) -> dict[str, object]:
    """Re-read a lost resolve race and use the same gate as the first attempt."""
    case = (
        conn.execute(
            text("SELECT status, invoice_ref FROM wb.cases WHERE id = :id"),
            {"id": case_id},
        )
        .mappings()
        .first()
    )
    if case is None:
        raise CaseNotFoundError(f"case {case_id} not found")
    current = current_staged(
        conn, invoice_ref=str(case["invoice_ref"]), attempt_id=attempt_id
    )
    decision = _resolution_decision(case, verdict, current, True)
    return {
        "case_id": case_id,
        "status": str(case["status"]),
        "resolved": False,
        "reason": decision.reason if not decision.allowed else "case-not-open",
    }


def _resolution_decision(
    case: RowMapping,
    verdict: RowMapping | None,
    current: RowMapping | None,
    audit_available: bool,
) -> ResolutionDecision:
    if str(case["status"]) != "open":
        return ResolutionDecision(False, "case-not-open")
    if verdict is None:
        return ResolutionDecision(False, "no-verdict")
    if current is None:
        return ResolutionDecision(False, "current-missing")
    return decide_resolution(
        ResolutionRequest(
            case_status=str(case["status"]),
            current_version=int(current["version"]),
            current_snapshot=str(current["snapshot"]),
            verdict_version=int(verdict["version"]),
            verdict_snapshot=str(verdict["snapshot"]),
            verdict_status=str(verdict["status"]),
            verdict_check_id=str(verdict["check_id"]),
            required_check_id=AMOUNT_CHECK_ID,
            report_complete=audit_available,
        )
    )


def resolve_case(
    *,
    intake: Engine,
    audit: Engine,
    case_id: str,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    resolved = scope or ExecutionScope.local()
    with scoped_transaction(intake, resolved, mutate=True, lock_staging=True) as conn:
        case = (
            conn.execute(
                text(
                    "SELECT status, invoice_ref, attempt_id"
                    " FROM wb.cases WHERE id = :id"
                ),
                {"id": case_id},
            )
            .mappings()
            .first()
        )
        if case is None:
            raise CaseNotFoundError(f"case {case_id} not found")
        if (
            resolved.attempt_id is not None
            and str(case["attempt_id"] or "") != resolved.attempt_id
        ):
            raise AttemptReadOnlyError("attempt-read-only")
        verdict = (
            conn.execute(
                text(
                    "SELECT submission_id, version, snapshot, run_id,"
                    " report_id, check_id, status, audit_event_id"
                    " FROM wb.case_verdicts WHERE case_id = :id"
                    " ORDER BY id DESC LIMIT 1"
                ),
                {"id": case_id},
            )
            .mappings()
            .first()
        )
        current = current_staged(
            conn, invoice_ref=str(case["invoice_ref"]), attempt_id=resolved.attempt_id
        )
        audit_view = (
            read_audit_lineage(audit, latest_verdict=verdict)
            if verdict is not None
            else None
        )
        decision = _resolution_decision(
            case,
            verdict,
            current,
            audit_view.report_available if audit_view is not None else False,
        )
        if not decision.allowed:
            return {
                "case_id": case_id,
                "status": str(case["status"]),
                "resolved": False,
                "reason": decision.reason,
            }
        assert verdict is not None
        updated = conn.execute(
            text(
                "UPDATE wb.cases SET status = 'resolved',"
                " resolved_at = NOW(), resolved_run_id = :run"
                " WHERE id = :id AND status = 'open'"
                " AND EXISTS (SELECT 1 FROM wb.staging_invoice"
                " WHERE invoice_ref = :ref AND version = :ver"
                " AND snapshot = :snap)"
            ),
            {
                "run": str(verdict["run_id"]),
                "id": case_id,
                "ref": str(case["invoice_ref"]),
                "ver": int(verdict["version"]),
                "snap": str(verdict["snapshot"]),
            },
        )
        if updated.rowcount != 1:
            return _refused_after_race(
                conn,
                case_id=case_id,
                verdict=verdict,
                attempt_id=resolved.attempt_id,
            )
        return {
            "case_id": case_id,
            "status": "resolved",
            "resolved": True,
            "reason": "ok",
            "resolved_run_id": str(verdict["run_id"]),
        }
