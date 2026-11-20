"""Exception case lifecycle, check orchestration, and resolution gate."""

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


class NothingStagedError(RuntimeError):
    """Raised when running checks with no Current Submission staged."""


class StaleStagedError(RuntimeError):
    """Raised when Current Submission changes during a check run."""


class CaseNotFoundError(RuntimeError):
    """Raised when an Exception Case or its bound evidence cannot be found."""


_GUIDANCE: dict[str, str] = {
    "ok": "checks pass on the current version, press Resolve case to close it.",
    "case-not-open": "none, this case is resolved.",
    "attempt-read-only": (
        "This Demo Attempt is read-only; return to the current Demo Attempt"
        " to make changes."
    ),
    "errored-report": (
        "The required check errored; inspect its message and rerun checks."
    ),
    "incomplete-report": (
        "The check report is incomplete or its audit trace is unavailable; "
        "run checks again, then resolve."
    ),
    "check-mismatch": (
        "The latest verdict is for a different check; run the required checks."
    ),
    "stale-version": (
        "A newer submission exists; run checks on the current version first."
    ),
    "snapshot-mismatch": (
        "The staged row changed since the check run; run checks again first."
    ),
    "verdict-not-pass": (
        "The latest verdict did not pass; ingest a corrected CSV "
        "or upload and confirm a corrected PDF, then run fresh checks."
    ),
    "no-verdict": "No check run is recorded for this case; run checks first.",
    "current-missing": (
        "This invoice is no longer staged; re-upload its latest CSV "
        "or confirm and submit its latest PDF, run checks, then resolve."
    ),
}


def _guidance_for(reason: str) -> str:
    return _GUIDANCE.get(reason, "Resolve was refused; run checks and try again.")


def _attempt_sql(attempt: str | None) -> tuple[str, dict[str, str]]:
    if attempt is None:
        return "", {}
    return " AND attempt_id = :attempt", {"attempt": attempt}


def _evaluate_resolution(
    case: RowMapping,
    verdict: RowMapping | None,
    current: RowMapping | None,
    *,
    audit_available: bool,
    attempt_read_only: bool,
) -> tuple[bool, str]:
    if str(case["status"]) != "open":
        return False, "case-not-open"
    if attempt_read_only:
        return False, "attempt-read-only"
    if verdict is None:
        return False, "no-verdict"
    if current is None:
        return False, "current-missing"
    if str(verdict["status"]) == "error":
        return False, "errored-report"
    if not audit_available:
        return False, "incomplete-report"
    if str(verdict["check_id"]) != AMOUNT_CHECK_ID:
        return False, "check-mismatch"
    if int(verdict["version"]) != int(current["version"]):
        return False, "stale-version"
    if str(verdict["snapshot"]) != str(current["snapshot"]):
        return False, "snapshot-mismatch"
    if str(verdict["status"]) != "pass":
        return False, "verdict-not-pass"
    return True, "ok"


def run_current_checks(
    *,
    intake: Engine,
    read_dsn: str,
    scope: ExecutionScope | None = None,
    _suite_runner: Callable[[str], Any] | None = None,
) -> dict[str, object]:
    """Run required checks against Current Submission and record verdict/case."""
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
    refused_reason: str | None = None,
) -> dict[str, object]:
    """Read an Exception Case, its check/audit history, and resolution state."""
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
    attempt_read_only = resolved.is_read_only_attempt or (
        resolved.attempt_id is not None
        and str(case["attempt_id"] or "") != resolved.attempt_id
    )
    allowed, reason = _evaluate_resolution(
        case,
        verdict,
        current,
        audit_available=audit_view.report_available,
        attempt_read_only=attempt_read_only,
    )
    return {
        "case_id": str(case["id"]),
        "invoice_ref": str(case["invoice_ref"]),
        "submission_id": str(case["source_submission_id"]),
        "source_version": int(source_version),
        "checked_submission_id": str(verdict["submission_id"]),
        "attempt_read_only": attempt_read_only,
        "resolution": {"allowed": allowed, "reason": reason},
        "guidance": _guidance_for(reason),
        "refusal": (
            {"reason": refused_reason, "guidance": _guidance_for(refused_reason)}
            if refused_reason
            else None
        ),
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
    audit: Engine,
    case_id: str,
    scope: ExecutionScope,
) -> dict[str, object]:
    """Re-read a lost resolve race and run the live resolution gate."""
    case = (
        conn.execute(
            text("SELECT status, invoice_ref, attempt_id FROM wb.cases WHERE id = :id"),
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
        conn, invoice_ref=str(case["invoice_ref"]), attempt_id=scope.attempt_id
    )
    audit_view = (
        read_audit_lineage(audit, latest_verdict=verdict)
        if verdict is not None
        else None
    )
    attempt_read_only = scope.is_read_only_attempt or (
        scope.attempt_id is not None
        and str(case["attempt_id"] or "") != scope.attempt_id
    )
    allowed, reason = _evaluate_resolution(
        case,
        verdict,
        current,
        audit_available=audit_view.report_available
        if audit_view is not None
        else False,
        attempt_read_only=attempt_read_only,
    )
    effective_reason = reason if not allowed else "case-not-open"
    return {
        "case_id": case_id,
        "status": str(case["status"]),
        "resolved": False,
        "reason": effective_reason,
        "guidance": _guidance_for(effective_reason),
    }


def resolve_case(
    *,
    intake: Engine,
    audit: Engine,
    case_id: str,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    """Resolve an open Exception Case if Current Submission has a fresh pass."""
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
        allowed, reason = _evaluate_resolution(
            case,
            verdict,
            current,
            audit_available=(
                audit_view.report_available if audit_view is not None else False
            ),
            attempt_read_only=False,
        )
        if not allowed:
            return {
                "case_id": case_id,
                "status": str(case["status"]),
                "resolved": False,
                "reason": reason,
                "guidance": _guidance_for(reason),
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
                audit=audit,
                case_id=case_id,
                scope=resolved,
            )
        return {
            "case_id": case_id,
            "status": "resolved",
            "resolved": True,
            "reason": "ok",
            "guidance": _guidance_for("ok"),
            "resolved_run_id": str(verdict["run_id"]),
        }
