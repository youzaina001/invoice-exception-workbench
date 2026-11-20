"""M1 slice: ingest submissions, run checks, present cases. Synchronous by design."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping

from invoice_triage.governance import (
    AMOUNT_CHECK_ID,
    execute_amount_check,
    read_audit_lineage,
)
from invoice_triage.request_scope import current_attempt, current_reviewer, set_attempt
from invoice_triage.resolution import (
    ResolutionDecision,
    ResolutionRequest,
    decide_resolution,
)

REQUIRED_COLUMNS = ("invoice_id", "supplier_id", "invoice_date", "currency", "amount")

# Serializes replacement of the singleton staging projection. Held as a
# transaction-scoped advisory lock, so it also works when staging is empty
# (SELECT FOR UPDATE cannot lock zero rows) and releases on commit/rollback.
# Keep lock identifiers stable across package renames so running versions agree.
_STAGING_LOCK_KEY = (
    int.from_bytes(hashlib.sha256(b"ledgerlens:staging").digest()[:8], "big") >> 1
)


class NothingStagedError(RuntimeError):
    pass


class StaleStagedError(RuntimeError):
    pass


class CaseNotFoundError(RuntimeError):
    pass


class AttemptNotFoundError(RuntimeError):
    pass


class AttemptReadOnlyError(RuntimeError):
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
    amount_minor = parse_amount_minor(raw_amount, filename=filename)
    return InvoiceRow(
        invoice_id=invoice_id,
        supplier_id=supplier_id,
        invoice_date=invoice_date,
        currency=currency,
        amount_minor=amount_minor,
    )


def parse_amount_minor(raw_amount: str, *, filename: str) -> int | None:
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
    return amount_minor


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


def _attempt_sql(attempt: str | None) -> tuple[str, dict[str, str]]:
    if attempt is None:
        return "", {}
    return " AND attempt_id = :attempt", {"attempt": attempt}


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
    attempt = current_attempt()
    if attempt is None:
        conn.execute(text("DELETE FROM wb.staging_invoice"))
    else:
        conn.execute(
            text("DELETE FROM wb.staging_invoice WHERE attempt_id = :attempt"),
            {"attempt": attempt},
        )
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
    extra, extra_params = _attempt_sql(current_attempt())
    latest = (
        conn.execute(
            text(
                "SELECT id FROM wb.submissions WHERE invoice_ref = :ref"
                + extra
                + " ORDER BY version DESC LIMIT 1"
            ),
            {"ref": invoice_ref, **extra_params},
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
    with engine.begin() as conn:
        return ingest_invoice_row(
            conn, filename=filename, row=row, content_hash=content_hash
        )


def ingest_invoice_row(
    conn: Connection, *, filename: str, row: InvoiceRow, content_hash: str
) -> dict[str, object]:
    """Store a validated row in the caller's transaction and existing staging lock."""
    invoice_ref = row.invoice_id
    snapshot = snapshot_row(row)
    _lock_staging(conn)
    extra, extra_params = _attempt_sql(current_attempt())
    existing = (
        conn.execute(
            text(
                "SELECT id, version FROM wb.submissions"
                " WHERE invoice_ref = :ref AND content_hash = :hash" + extra
            ),
            {"ref": invoice_ref, "hash": content_hash, **extra_params},
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
                " FROM wb.submissions WHERE invoice_ref = :ref" + extra
            ),
            {"ref": invoice_ref, **extra_params},
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
    reviewer = current_reviewer()
    if reviewer is not None:
        _lock_workspace(conn, reviewer)
        latest = conn.execute(
            text(
                "SELECT id FROM wb.demo_attempts WHERE reviewer_id = :r"
                " ORDER BY created_at DESC, id DESC LIMIT 1"
            ),
            {"r": reviewer},
        ).scalar_one_or_none()
        if latest != current_attempt():
            raise AttemptReadOnlyError("attempt-read-only")
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _STAGING_LOCK_KEY})


def _current_staged(
    conn: Connection,
    invoice_ref: str | None = None,
    *,
    attempt: str | None = None,
    bound: bool = False,
) -> RowMapping | None:
    query = (
        "SELECT invoice_ref, submission_id, version, snapshot FROM wb.staging_invoice"
    )
    clauses: list[str] = []
    params: dict[str, str] = {}
    if invoice_ref is not None:
        clauses.append("invoice_ref = :ref")
        params["ref"] = invoice_ref
    if not bound:
        attempt = current_attempt()
    if attempt is not None:
        clauses.append("attempt_id = :attempt")
        params["attempt"] = attempt
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    return conn.execute(text(query + " LIMIT 1"), params).mappings().first()


def run_current_checks(
    *,
    intake: Engine,
    read_dsn: str,
    _suite_runner: Callable[[str], Any] | None = None,
) -> dict[str, object]:
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
        checked = execute_amount_check(read_dsn, _suite_runner=_suite_runner)
        status = checked.status
        if _current_staged(conn) != staged:
            raise StaleStagedError("staging changed during the check run; rerun checks")
        extra, extra_params = _attempt_sql(current_attempt())
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
        history_sql = (
            "SELECT m.version, m.id, r.invoice_id, r.supplier_id,"
            " r.invoice_date, r.currency, r.amount_minor,"
            " (SELECT p.fields #>> '{amount_minor,state}'"
            " FROM wb.pdf_intakes i JOIN wb.pdf_revisions p"
            " ON p.draft_id = i.draft_id AND p.revision = i.revision"
            " WHERE i.submission_id = m.id LIMIT 1) AS amount_state"
            " FROM wb.submissions m"
            " JOIN wb.submission_rows r ON r.submission_id = m.id"
            " WHERE m.invoice_ref = :ref"
        )
        history_params: dict[str, str] = {"ref": str(case["invoice_ref"])}
        case_attempt = (
            str(case["attempt_id"]) if case["attempt_id"] is not None else None
        )
        if case_attempt is not None:
            history_sql += " AND m.attempt_id = :attempt"
            history_params["attempt"] = case_attempt
        history_rows = (
            conn.execute(text(history_sql + " ORDER BY m.version"), history_params)
            .mappings()
            .all()
        )
        amount_states = {str(h["id"]): h["amount_state"] for h in history_rows}
        current = _current_staged(
            conn, str(case["invoice_ref"]), attempt=case_attempt, bound=True
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
    if current_attempt() is not None and case["attempt_id"] != current_attempt():
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
        "row": _row_dict(stored),
        "amount_state": amount_states[str(verdict["submission_id"])],
        "core": {
            "n_failed": verdict["n_failed"],
            "n_total": verdict["n_total"],
            "message": verdict["message"],
            "sample_failures": verdict["sample_failures"],
        },
        "audit": audit_view.labels,
        "history": {
            "audit": audit_view.events,
            "submissions": [
                {
                    "version": int(h["version"]),
                    "submission_id": str(h["id"]),
                    "row": _row_dict(h),
                    "amount_state": h["amount_state"],
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


def _lock_workspace(conn: Connection, reviewer_id: str) -> None:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:reviewer, 0))"),
        {"reviewer": "ledgerlens:workspace:" + reviewer_id},
    )


def ensure_demo_workspace(engine: Engine, reviewer_id: str) -> dict[str, str]:
    """Create this Reviewer's Demo Workspace and first empty Demo Attempt."""
    with engine.begin() as conn:
        _lock_workspace(conn, reviewer_id)
        existing = (
            conn.execute(
                text("SELECT id FROM wb.demo_workspaces WHERE reviewer_id = :r"),
                {"r": reviewer_id},
            )
            .mappings()
            .first()
        )
        if existing is None:
            workspace_id = uuid4().hex
            conn.execute(
                text(
                    "INSERT INTO wb.demo_workspaces (id, reviewer_id) VALUES (:id, :r)"
                ),
                {"id": workspace_id, "r": reviewer_id},
            )
        else:
            workspace_id = str(existing["id"])
        attempt = (
            conn.execute(
                text(
                    "SELECT id FROM wb.demo_attempts WHERE workspace_id = :w"
                    " ORDER BY created_at DESC, id DESC LIMIT 1"
                ),
                {"w": workspace_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            attempt_id = uuid4().hex
            conn.execute(
                text(
                    "INSERT INTO wb.demo_attempts"
                    " (id, workspace_id, reviewer_id, created_at)"
                    " VALUES (:id, :w, :r, clock_timestamp())"
                ),
                {"id": attempt_id, "w": workspace_id, "r": reviewer_id},
            )
        else:
            attempt_id = str(attempt["id"])
    return {"workspace_id": workspace_id, "attempt_id": attempt_id}


_MAX_DEMO_ATTEMPTS = 5


def _delete_demo_attempt(conn: Connection, attempt_id: str) -> None:
    """Remove one attempt and the rows that belong only to it.

    Audit tables are append-only for the request role, so the check runs and
    traces go through a definer function that deletes by this attempt only.
    """
    params = {"attempt": attempt_id}
    for statement in (
        "DELETE FROM wb.case_verdicts WHERE attempt_id = :attempt",
        "DELETE FROM wb.cases WHERE attempt_id = :attempt",
        "DELETE FROM wb.submission_rows WHERE attempt_id = :attempt",
        "DELETE FROM wb.staging_invoice WHERE attempt_id = :attempt",
        "DELETE FROM wb.submissions WHERE attempt_id = :attempt",
    ):
        conn.execute(text(statement), params)
    conn.execute(text("SELECT wb.delete_attempt_audit(:attempt)"), params)
    conn.execute(text("DELETE FROM wb.demo_attempts WHERE id = :attempt"), params)


def start_fresh_attempt(engine: Engine, reviewer_id: str) -> dict[str, str]:
    """Open a new current Demo Attempt. A workspace keeps at most five."""
    # Staging visibility follows the current attempt. Clearing it lets this
    # transaction remove the oldest attempt's staging row and nothing else.
    set_attempt(None)
    with engine.begin() as conn:
        _lock_workspace(conn, reviewer_id)
        workspace = (
            conn.execute(
                text("SELECT id FROM wb.demo_workspaces WHERE reviewer_id = :r"),
                {"r": reviewer_id},
            )
            .mappings()
            .one()
        )
        workspace_id = str(workspace["id"])
        attempts = (
            conn.execute(
                text(
                    "SELECT id FROM wb.demo_attempts WHERE workspace_id = :w"
                    " ORDER BY created_at ASC, id ASC"
                ),
                {"w": workspace_id},
            )
            .mappings()
            .all()
        )
        previous_id = str(attempts[-1]["id"])
        attempt_id = uuid4().hex
        conn.execute(
            text(
                "INSERT INTO wb.demo_attempts"
                " (id, workspace_id, reviewer_id, created_at)"
                " VALUES (:id, :w, :r, clock_timestamp())"
            ),
            {"id": attempt_id, "w": workspace_id, "r": reviewer_id},
        )
        if len(attempts) >= _MAX_DEMO_ATTEMPTS:
            _delete_demo_attempt(conn, str(attempts[0]["id"]))
    return {
        "workspace_id": workspace_id,
        "attempt_id": attempt_id,
        "previous_attempt_id": previous_id,
    }


def list_demo_attempts(engine: Engine) -> list[dict[str, str]]:
    with engine.connect() as conn:
        return [
            {"id": row.id, "created_at": row.created_at.isoformat()}
            for row in conn.execute(
                text(
                    "SELECT id, created_at FROM wb.demo_attempts"
                    " ORDER BY created_at DESC, id DESC"
                )
            )
        ]


def read_demo_attempt(
    *, intake: Engine, audit: Engine, attempt_id: str
) -> dict[str, object]:
    """Read one Demo Attempt, including a previous read-only one."""
    with intake.connect() as conn:
        attempt = (
            conn.execute(
                text("SELECT id, workspace_id FROM wb.demo_attempts WHERE id = :id"),
                {"id": attempt_id},
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise AttemptNotFoundError(f"attempt {attempt_id} not found")
        verdicts = (
            conn.execute(
                text(
                    "SELECT case_id, run_id, check_id, status"
                    " FROM wb.case_verdicts WHERE attempt_id = :attempt"
                    " ORDER BY id"
                ),
                {"attempt": attempt_id},
            )
            .mappings()
            .all()
        )
    audit_view = read_audit_lineage(audit, attempt_id=attempt_id)
    return {
        "workspace_id": str(attempt["workspace_id"]),
        "attempt_id": str(attempt["id"]),
        **list_overview(intake, attempt_id=attempt_id),
        "check_results": [
            {
                "case_id": row["case_id"],
                "run_id": row["run_id"],
                "check_id": row["check_id"],
                "status": row["status"],
            }
            for row in verdicts
        ],
        "audit": audit_view.events,
    }


def list_overview(
    engine: Engine, *, attempt_id: str | None = None
) -> dict[str, object]:
    submission_sql = (
        "SELECT id, invoice_ref, version, filename, ingested_at FROM wb.submissions"
    )
    case_sql = "SELECT id, invoice_ref, check_id, status, created_at FROM wb.cases"
    params: dict[str, str] = {}
    if attempt_id is not None:
        submission_sql += " WHERE attempt_id = :attempt"
        case_sql += " WHERE attempt_id = :attempt"
        params["attempt"] = attempt_id
    with engine.connect() as conn:
        submissions = (
            conn.execute(
                text(submission_sql + " ORDER BY ingested_at DESC"),
                params,
            )
            .mappings()
            .all()
        )
        cases = (
            conn.execute(text(case_sql + " ORDER BY created_at DESC"), params)
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
    *, intake: Engine, audit: Engine, case_id: str, attempt_id: str | None = None
) -> dict[str, object]:
    with intake.begin() as conn:
        _lock_staging(conn)
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
        if attempt_id is not None and str(case["attempt_id"] or "") != attempt_id:
            return {
                "case_id": case_id,
                "status": str(case["status"]),
                "resolved": False,
                "reason": "attempt-read-only",
            }
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
            )
        return {
            "case_id": case_id,
            "status": "resolved",
            "resolved": True,
            "reason": "ok",
            "resolved_run_id": str(verdict["run_id"]),
        }
