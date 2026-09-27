"""M1 slice: ingest submissions, run checks, present cases. Synchronous by design."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from airflow_dq_agent.quality.suite import run_quality_suite
from airflow_dq_agent.traces.repository import PostgresAuditRepository
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from ledgerlens.core_integration import AMOUNT_CHECK_ID, ensure_registered
from ledgerlens.resolution import (
    ResolutionDecision,
    ResolutionRequest,
    decide_resolution,
)

REQUIRED_COLUMNS = ("invoice_id", "supplier_id", "invoice_date", "currency", "amount")

# Serializes replacement of the singleton staging projection. Held as a
# transaction-scoped advisory lock, so it also works when staging is empty
# (SELECT FOR UPDATE cannot lock zero rows) and releases on commit/rollback.
_STAGING_LOCK_KEY = (
    int.from_bytes(hashlib.sha256(b"ledgerlens:staging").digest()[:8], "big") >> 1
)


class NothingStagedError(RuntimeError):
    pass


class StaleStagedError(RuntimeError):
    pass


class CheckMissingError(RuntimeError):
    pass


class CaseNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class InvoiceRow:
    invoice_id: str
    supplier_id: str
    invoice_date: date
    currency: str
    amount_minor: int | None


def parse_submission_csv(data: bytes, *, filename: str) -> InvoiceRow:
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename}: file must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(decoded))
    if reader.fieldnames != list(REQUIRED_COLUMNS):
        raise ValueError(f"{filename}: expected header {','.join(REQUIRED_COLUMNS)}")
    rows = []
    for record in reader:
        if record.get(None):
            raise ValueError(f"{filename}: expected 5 columns, found extra fields")
        if any((cell or "").strip() for cell in record.values()):
            rows.append(record)
    if len(rows) != 1:
        raise ValueError(f"{filename}: M1 supports exactly one invoice per submission")
    row = rows[0]
    invoice_id = (row["invoice_id"] or "").strip()
    supplier_id = (row["supplier_id"] or "").strip()
    currency = (row["currency"] or "").strip()
    if not invoice_id or not supplier_id or not currency:
        raise ValueError(
            f"{filename}: invoice_id, supplier_id, and currency are required"
        )
    try:
        invoice_date = date.fromisoformat((row["invoice_date"] or "").strip())
    except ValueError as exc:
        raise ValueError(f"{filename}: invoice_date must be YYYY-MM-DD") from exc
    raw_amount = (row["amount"] or "").strip()
    if not raw_amount:
        amount_minor: int | None = None
    else:
        try:
            cents = Decimal(raw_amount) * 100
        except InvalidOperation as exc:
            raise ValueError(
                f"{filename}: amount {raw_amount!r} is not decimal"
            ) from exc
        if not cents.is_finite():
            raise ValueError(f"{filename}: amount {raw_amount!r} is not finite")
        if cents != cents.to_integral_value():
            raise ValueError(
                f"{filename}: amount {raw_amount!r} has sub-cent precision"
            )
        amount_minor = int(cents)
        if amount_minor < 0:
            raise ValueError(f"{filename}: amount must not be negative")
        if amount_minor > 9_223_372_036_854_775_807:
            raise ValueError(
                f"{filename}: amount {raw_amount!r} exceeds supported range"
            )
    return InvoiceRow(
        invoice_id=invoice_id,
        supplier_id=supplier_id,
        invoice_date=invoice_date,
        currency=currency,
        amount_minor=amount_minor,
    )


def snapshot_row(row: InvoiceRow) -> str:
    canonical = json.dumps(
        {
            "invoice_id": row.invoice_id,
            "supplier_id": row.supplier_id,
            "invoice_date": row.invoice_date.isoformat(),
            "currency": row.currency,
            "amount_minor": row.amount_minor,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _submission_payload(
    submission_id: str,
    invoice_ref: str,
    version: int,
    duplicate: bool,
    made_current: bool,
) -> dict[str, object]:
    return {
        "submission_id": submission_id,
        "invoice_ref": invoice_ref,
        "version": version,
        "duplicate": duplicate,
        "made_current": made_current,
    }


def _replace_staging(
    conn: Connection,
    *,
    invoice_ref: str,
    submission_id: str,
    version: int,
    row: InvoiceRow,
    snapshot: str,
) -> None:
    """Replace the singleton staging projection with one submission.

    The caller must hold the staging advisory lock: every staging write
    runs inside the ingest transaction, which takes it first.
    """
    # Staging holds exactly the current submission, so the suite verdict
    # can never mix rows from other invoices or versions.
    conn.execute(text("DELETE FROM wb.staging_invoice"))
    conn.execute(
        text(
            "INSERT INTO wb.staging_invoice"
            " (invoice_ref, submission_id, version, invoice_id, supplier_id,"
            " invoice_date, currency, amount_minor, snapshot)"
            " VALUES (:ref, :sid, :ver, :iid, :sup, :dt, :cur, :amt, :snap)"
        ),
        {
            "ref": invoice_ref,
            "sid": submission_id,
            "ver": version,
            "iid": row.invoice_id,
            "sup": row.supplier_id,
            "dt": row.invoice_date,
            "cur": row.currency,
            "amt": row.amount_minor,
            "snap": snapshot,
        },
    )


def _restage_if_latest(
    conn: Connection, invoice_ref: str, submission_id: str, version: int
) -> bool:
    """Restage a duplicate upload when it is the invoice's latest version.

    History is untouched (no new version); only the singleton staging
    projection moves. Returns True when the submission became current.
    """
    latest = (
        conn.execute(
            text(
                "SELECT id FROM wb.submissions WHERE invoice_ref = :ref"
                " ORDER BY version DESC LIMIT 1"
            ),
            {"ref": invoice_ref},
        )
        .mappings()
        .first()
    )
    if latest is None or str(latest["id"]) != submission_id:
        return False
    stored = (
        conn.execute(
            text(
                "SELECT invoice_id, supplier_id, invoice_date, currency,"
                " amount_minor FROM wb.submission_rows"
                " WHERE submission_id = :sid"
            ),
            {"sid": submission_id},
        )
        .mappings()
        .first()
    )
    if stored is None:
        return False
    row = InvoiceRow(
        invoice_id=str(stored["invoice_id"]),
        supplier_id=str(stored["supplier_id"]),
        invoice_date=stored["invoice_date"],
        currency=str(stored["currency"]),
        amount_minor=stored["amount_minor"],
    )
    _replace_staging(
        conn,
        invoice_ref=invoice_ref,
        submission_id=submission_id,
        version=version,
        row=row,
        snapshot=snapshot_row(row),
    )
    return True


def ingest_submission(
    engine: Engine, *, filename: str, data: bytes
) -> dict[str, object]:
    row = parse_submission_csv(data, filename=filename)
    content_hash = hashlib.sha256(data).hexdigest()
    invoice_ref = row.invoice_id
    snapshot = snapshot_row(row)
    with engine.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _STAGING_LOCK_KEY},
        )
        existing = (
            conn.execute(
                text(
                    "SELECT id, version FROM wb.submissions"
                    " WHERE invoice_ref = :ref AND content_hash = :hash"
                ),
                {"ref": invoice_ref, "hash": content_hash},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            made_current = _restage_if_latest(
                conn,
                invoice_ref,
                str(existing["id"]),
                int(existing["version"]),
            )
            return _submission_payload(
                str(existing["id"]),
                invoice_ref,
                int(existing["version"]),
                True,
                made_current,
            )
        prior = (
            conn.execute(
                text(
                    "SELECT COALESCE(MAX(version), 0) AS max_version"
                    " FROM wb.submissions WHERE invoice_ref = :ref"
                ),
                {"ref": invoice_ref},
            )
            .mappings()
            .first()
        )
        version = int(prior["max_version"]) + 1 if prior else 1
        submission_id = uuid4().hex
        conn.execute(
            text(
                "INSERT INTO wb.submissions"
                " (id, invoice_ref, version, content_hash, filename)"
                " VALUES (:id, :ref, :version, :hash, :filename)"
            ),
            {
                "id": submission_id,
                "ref": invoice_ref,
                "version": version,
                "hash": content_hash,
                "filename": filename,
            },
        )
        conn.execute(
            text(
                "INSERT INTO wb.submission_rows"
                " (submission_id, invoice_id, supplier_id, invoice_date,"
                " currency, amount_minor)"
                " VALUES (:sid, :iid, :sup, :dt, :cur, :amt)"
            ),
            {
                "sid": submission_id,
                "iid": row.invoice_id,
                "sup": row.supplier_id,
                "dt": row.invoice_date,
                "cur": row.currency,
                "amt": row.amount_minor,
            },
        )
        _replace_staging(
            conn,
            invoice_ref=invoice_ref,
            submission_id=submission_id,
            version=version,
            row=row,
            snapshot=snapshot,
        )
        return _submission_payload(submission_id, invoice_ref, version, False, True)


def _current_staged(engine: Engine) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT invoice_ref, submission_id, version, snapshot"
                    " FROM wb.staging_invoice LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row is not None else None


def run_current_checks(*, intake: Engine, read_dsn: str) -> dict[str, object]:
    ensure_registered()
    # ponytail: serialize the singleton staging journey; use per-invoice
    # staging and locks if simultaneous invoice checks become necessary.
    with intake.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _STAGING_LOCK_KEY},
        )
        staged = _current_staged(intake)
        if staged is None:
            raise NothingStagedError("no submission staged; ingest a CSV first")
        invoice_ref = str(staged["invoice_ref"])
        submission_id = str(staged["submission_id"])
        version = int(staged["version"])
        snapshot = str(staged["snapshot"])
        report = run_quality_suite(read_dsn)
        result = report.get(AMOUNT_CHECK_ID)
        if result is None:
            raise CheckMissingError(f"required check {AMOUNT_CHECK_ID} did not run")
        status = result.status.value
        restaged = (
            conn.execute(
                text(
                    "SELECT invoice_ref, submission_id, version, snapshot"
                    " FROM wb.staging_invoice LIMIT 1"
                )
            )
            .mappings()
            .first()
        )
        if (
            restaged is None
            or str(restaged["invoice_ref"]) != invoice_ref
            or str(restaged["submission_id"]) != submission_id
            or int(restaged["version"]) != version
            or str(restaged["snapshot"]) != snapshot
        ):
            raise StaleStagedError("staging changed during the check run; rerun checks")
        case_row = (
            conn.execute(
                text(
                    "SELECT id FROM wb.cases"
                    " WHERE invoice_ref = :ref AND check_id = :check"
                    " AND status = 'open'"
                ),
                {"ref": invoice_ref, "check": AMOUNT_CHECK_ID},
            )
            .mappings()
            .first()
        )
        if case_row is None and status != "fail":
            return {
                "submission_id": submission_id,
                "invoice_ref": invoice_ref,
                "version": version,
                "run_id": report.run_id,
                "report_id": report.report_id,
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
                "run": report.run_id,
                "rep": report.report_id,
                "check": AMOUNT_CHECK_ID,
                "status": status,
                "evt": report.audit_event_id,
                "nfail": result.n_failed,
                "ntot": result.n_total,
                "msg": result.message,
                "samples": json.dumps(result.sample_failures),
            },
        )
        return {
            "submission_id": submission_id,
            "invoice_ref": invoice_ref,
            "version": version,
            "run_id": report.run_id,
            "report_id": report.report_id,
            "check_id": AMOUNT_CHECK_ID,
            "status": status,
            "case_id": case_id,
            "case_status": "open",
        }


def _row_dict(row: RowMapping) -> dict[str, object]:
    return {
        "invoice_id": str(row["invoice_id"]),
        "supplier_id": str(row["supplier_id"]),
        "invoice_date": row["invoice_date"].isoformat(),
        "currency": str(row["currency"]),
        "amount_minor": row["amount_minor"],
    }


def get_case_detail(
    *, intake: Engine, audit_dsn: str, case_id: str
) -> dict[str, object]:
    with intake.connect() as conn:
        case = (
            conn.execute(
                text(
                    "SELECT id, invoice_ref, source_submission_id, check_id, status,"
                    " created_at, resolved_at FROM wb.cases WHERE id = :id"
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
        stored = (
            conn.execute(
                text(
                    "SELECT invoice_id, supplier_id, invoice_date, currency,"
                    " amount_minor"
                    " FROM wb.submission_rows WHERE submission_id = :sid"
                ),
                {"sid": str(verdict["submission_id"])},
            )
            .mappings()
            .first()
        )
        if stored is None:
            raise CaseNotFoundError(f"case {case_id} evidence missing")
        history_rows = (
            conn.execute(
                text(
                    "SELECT m.version, m.id, r.invoice_id, r.supplier_id,"
                    " r.invoice_date, r.currency, r.amount_minor"
                    " FROM wb.submissions m"
                    " JOIN wb.submission_rows r ON r.submission_id = m.id"
                    " WHERE m.invoice_ref = :ref ORDER BY m.version"
                ),
                {"ref": str(case["invoice_ref"])},
            )
            .mappings()
            .all()
        )
        current = (
            conn.execute(
                text(
                    "SELECT version, snapshot FROM wb.staging_invoice"
                    " WHERE invoice_ref = :ref"
                ),
                {"ref": str(case["invoice_ref"])},
            )
            .mappings()
            .first()
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
    audit_available = _audit_available(verdict, audit_dsn)
    quality_report = "recorded" if audit_available else "unknown"
    decision = _resolution_decision(case, verdict, current, audit_available)
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
        "row": _row_dict(stored),
        "core": {
            "n_failed": verdict["n_failed"],
            "n_total": verdict["n_total"],
            "message": verdict["message"],
            "sample_failures": verdict["sample_failures"],
        },
        "audit": {
            "quality_report": quality_report,
            "shadow_review": "unknown",
            "approval": "unknown",
            "human_decision": "unknown",
            "quarantine": "unknown",
            "rejection": "unknown",
            "timeout": "unknown",
        },
        "history": {
            "submissions": [
                {
                    "version": int(h["version"]),
                    "submission_id": str(h["id"]),
                    "row": _row_dict(h),
                }
                for h in history_rows
            ],
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


def list_overview(engine: Engine) -> dict[str, object]:
    with engine.connect() as conn:
        submissions = (
            conn.execute(
                text(
                    "SELECT id, invoice_ref, version, filename, ingested_at"
                    " FROM wb.submissions ORDER BY ingested_at DESC"
                )
            )
            .mappings()
            .all()
        )
        cases = (
            conn.execute(
                text(
                    "SELECT id, invoice_ref, check_id, status, created_at"
                    " FROM wb.cases ORDER BY created_at DESC"
                )
            )
            .mappings()
            .all()
        )
    return {
        "submissions": [
            {
                "id": r["id"],
                "invoice_ref": r["invoice_ref"],
                "version": r["version"],
                "filename": r["filename"],
                "ingested_at": r["ingested_at"].isoformat(),
            }
            for r in submissions
        ],
        "cases": [
            {
                "id": r["id"],
                "invoice_ref": r["invoice_ref"],
                "check_id": r["check_id"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat(),
            }
            for r in cases
        ],
    }


def _refused_after_race(
    conn: Connection,
    *,
    case_id: str,
    invoice_ref: str,
    verdict_version: int,
    verdict_snapshot: str,
) -> dict[str, object]:
    """Explain a lost resolve race by re-reading current state."""
    status_row = (
        conn.execute(
            text("SELECT status FROM wb.cases WHERE id = :id"),
            {"id": case_id},
        )
        .mappings()
        .first()
    )
    status = str(status_row["status"]) if status_row is not None else "open"
    if status != "open":
        reason = "case-not-open"
    else:
        current = (
            conn.execute(
                text(
                    "SELECT version, snapshot FROM wb.staging_invoice"
                    " WHERE invoice_ref = :ref"
                ),
                {"ref": invoice_ref},
            )
            .mappings()
            .first()
        )
        if current is None:
            reason = "current-missing"
        elif int(current["version"]) != verdict_version:
            reason = "stale-version"
        elif str(current["snapshot"]) != verdict_snapshot:
            reason = "snapshot-mismatch"
        else:
            reason = "case-not-open"
    return {
        "case_id": case_id,
        "status": status,
        "resolved": False,
        "reason": reason,
    }


def _audit_available(verdict: RowMapping | None, audit_dsn: str) -> bool:
    if verdict is None or verdict["audit_event_id"] is None:
        return False
    try:
        event = PostgresAuditRepository(audit_dsn).get(str(verdict["audit_event_id"]))
    except (SQLAlchemyError, ValueError):
        return False
    return (
        event is not None
        and event.event_id == str(verdict["audit_event_id"])
        and event.kind == "quality_report"
        and event.quality_run_id == str(verdict["run_id"])
        and event.report_id == str(verdict["report_id"])
    )


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
            report_complete=audit_available and str(verdict["status"]) != "error",
        )
    )


def resolve_case(*, intake: Engine, audit_dsn: str, case_id: str) -> dict[str, object]:
    with intake.begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _STAGING_LOCK_KEY},
        )
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
        current = (
            conn.execute(
                text(
                    "SELECT version, snapshot FROM wb.staging_invoice"
                    " WHERE invoice_ref = :ref"
                ),
                {"ref": str(case["invoice_ref"])},
            )
            .mappings()
            .first()
        )
        decision = _resolution_decision(
            case, verdict, current, _audit_available(verdict, audit_dsn)
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
                invoice_ref=str(case["invoice_ref"]),
                verdict_version=int(verdict["version"]),
                verdict_snapshot=str(verdict["snapshot"]),
            )
        return {
            "case_id": case_id,
            "status": "resolved",
            "resolved": True,
            "reason": "ok",
            "resolved_run_id": str(verdict["run_id"]),
        }
