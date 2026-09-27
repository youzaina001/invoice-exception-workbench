"""M1 slice: ingest submissions, run checks, present cases. Synchronous by design."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from uuid import uuid4

from airflow_dq_agent.contracts.models import AuditEvent
from airflow_dq_agent.quality.suite import run_quality_suite
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping

from ledgerlens.audit import audit_labels, read_audit_events
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
            amount = Decimal(raw_amount)
        except InvalidOperation as exc:
            raise ValueError(
                f"{filename}: amount {raw_amount!r} is not decimal"
            ) from exc
        if not amount.is_finite():
            raise ValueError(f"{filename}: amount {raw_amount!r} is not finite")
        if amount < 0 or amount > Decimal("92233720368547758.07"):
            raise ValueError(f"{filename}: amount is outside the supported range")
        # Reject on scale, not on a rounded product: the default decimal
        # context would otherwise round long inputs into an accepted value.
        exponent = amount.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -2:
            raise ValueError(
                f"{filename}: amount {raw_amount!r} has sub-cent precision"
            )
        with localcontext() as ctx:
            ctx.prec = max(len(raw_amount), 28) + 2
            cents = amount * 100
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
        _lock_staging(conn)
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


def _lock_staging(conn: Connection) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGING_LOCK_KEY})


def _current_staged(
    conn: Connection, invoice_ref: str | None = None
) -> RowMapping | None:
    query = (
        "SELECT invoice_ref, submission_id, version, snapshot FROM wb.staging_invoice"
    )
    if invoice_ref is not None:
        query += " WHERE invoice_ref = :ref"
    return (
        conn.execute(text(query + " LIMIT 1"), {"ref": invoice_ref}).mappings().first()
    )


def run_current_checks(*, intake: Engine, read_dsn: str) -> dict[str, object]:
    ensure_registered()
    # ponytail: serialize the singleton staging journey; use per-invoice
    # staging and locks if simultaneous invoice checks become necessary.
    with intake.begin() as conn:
        _lock_staging(conn)
        staged = _current_staged(conn)
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
        if _current_staged(conn) != staged:
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
        if case_row is None and status == "pass":
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
    *, intake: Engine, audit: Engine, case_id: str
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
        current = _current_staged(conn, str(case["invoice_ref"]))
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
    events = read_audit_events(audit, [str(v["run_id"]) for v in history_verdicts])
    latest_events = [e for e in events if e.quality_run_id == str(verdict["run_id"])]
    audit_available = _audit_available(verdict, events)
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
        "audit": audit_labels(latest_events, audit_available),
        "history": {
            "audit": [
                {
                    "event_id": e.event_id,
                    "run_id": e.quality_run_id,
                    "kind": e.kind,
                    "label": e.kind.replace("_", " ").capitalize(),
                    "occurred_at": e.occurred_at.isoformat(),
                }
                for e in events
            ],
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
    conn: Connection, *, case_id: str, verdict: RowMapping
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
    current = _current_staged(conn, str(case["invoice_ref"]))
    decision = _resolution_decision(case, verdict, current, True)
    return {
        "case_id": case_id,
        "status": str(case["status"]),
        "resolved": False,
        "reason": decision.reason if not decision.allowed else "case-not-open",
    }


def _audit_available(verdict: RowMapping | None, events: list[AuditEvent]) -> bool:
    if verdict is None or verdict["audit_event_id"] is None:
        return False
    return any(
        event.event_id == str(verdict["audit_event_id"])
        and event.kind == "quality_report"
        and event.quality_run_id == str(verdict["run_id"])
        and event.report_id == str(verdict["report_id"])
        for event in events
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
            report_complete=audit_available,
        )
    )


def resolve_case(*, intake: Engine, audit: Engine, case_id: str) -> dict[str, object]:
    with intake.begin() as conn:
        _lock_staging(conn)
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
        current = _current_staged(conn, str(case["invoice_ref"]))
        events = read_audit_events(audit, [str(verdict["run_id"])]) if verdict else []
        decision = _resolution_decision(
            case, verdict, current, _audit_available(verdict, events)
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
            )
        return {
            "case_id": case_id,
            "status": "resolved",
            "resolved": True,
            "reason": "ok",
            "resolved_run_id": str(verdict["run_id"]),
        }
