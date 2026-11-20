"""Submission intake, strict amount parsing, and singleton staging."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping

from invoice_triage.deterministic import amount_value
from invoice_triage.extraction import canonical, digest
from invoice_triage.request_scope import (
    AttemptReadOnlyError,
    ExecutionScope,
    acquire_staging_lock,
    scoped_transaction,
)

_REQUIRED_COLUMNS = ("invoice_id", "supplier_id", "invoice_date", "currency", "amount")


@dataclass(frozen=True)
class InvoiceRow:
    """Normalized invoice fields staged for quality evaluation."""

    invoice_id: str
    supplier_id: str
    invoice_date: date
    currency: str
    amount_minor: int | None


def parse_amount_minor(raw_amount: str, *, filename: str) -> int | None:
    """Parse an optional unsigned decimal-dot amount into minor currency units."""
    if not raw_amount:
        return None
    parsed = amount_value(raw_amount)
    if parsed is None:
        raise ValueError(
            f"{filename}: amount {raw_amount!r} must be an unsigned decimal"
            " with up to two fractional digits within supported range"
        )
    return parsed


def _parse_submission_csv(data: bytes, *, filename: str) -> InvoiceRow:
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{filename}: file must be UTF-8 CSV") from exc
    reader = csv.DictReader(io.StringIO(decoded))
    if reader.fieldnames != list(_REQUIRED_COLUMNS):
        raise ValueError(f"{filename}: expected header {','.join(_REQUIRED_COLUMNS)}")
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


def _snapshot_row(row: InvoiceRow) -> str:
    return digest(
        canonical(
            {
                "invoice_id": row.invoice_id,
                "supplier_id": row.supplier_id,
                "invoice_date": row.invoice_date.isoformat(),
                "currency": row.currency,
                "amount_minor": row.amount_minor,
            }
        )
    )


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


def _attempt_sql(
    attempt: str | None, *, alias: str | None = None
) -> tuple[str, dict[str, str]]:
    if attempt is None:
        return "", {}
    column = f"{alias}.attempt_id" if alias else "attempt_id"
    return f" AND {column} = :attempt", {"attempt": attempt}


def _row_dict(row: RowMapping) -> dict[str, object]:
    return {
        "invoice_id": str(row["invoice_id"]),
        "supplier_id": str(row["supplier_id"]),
        "invoice_date": row["invoice_date"].isoformat(),
        "currency": str(row["currency"]),
        "amount_minor": row["amount_minor"],
    }


def _replace_staging(
    conn: Connection,
    *,
    invoice_ref: str,
    submission_id: str,
    version: int,
    row: InvoiceRow,
    snapshot: str,
    attempt_id: str | None = None,
) -> None:
    """Replace the singleton staging projection with one submission.

    The caller must hold the staging advisory lock: every staging write
    runs inside the ingest transaction, which takes it first.
    """
    # Staging holds exactly the current submission, so the suite verdict
    # can never mix rows from other invoices or versions.
    if attempt_id is None:
        conn.execute(text("DELETE FROM wb.staging_invoice"))
    else:
        conn.execute(
            text("DELETE FROM wb.staging_invoice WHERE attempt_id = :attempt"),
            {"attempt": attempt_id},
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
    conn: Connection,
    invoice_ref: str,
    submission_id: str,
    version: int,
    *,
    attempt_id: str | None = None,
) -> bool:
    """Restage a duplicate upload when it is the invoice's latest version.

    History is untouched (no new version); only the singleton staging
    projection moves. Returns True when the submission became current.
    """
    extra, extra_params = _attempt_sql(attempt_id)
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
        snapshot=_snapshot_row(row),
        attempt_id=attempt_id,
    )
    return True


def ingest_submission(
    engine: Engine,
    *,
    filename: str,
    data: bytes,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    """Validate and stage a single-invoice CSV upload in the caller's scope."""
    resolved = scope or ExecutionScope.local()
    if resolved.is_read_only_attempt:
        raise AttemptReadOnlyError("attempt-read-only")
    row = _parse_submission_csv(data, filename=filename)
    content_hash = digest(data)
    with scoped_transaction(engine, resolved, mutate=True, lock_staging=True) as conn:
        return ingest_invoice_row(
            conn,
            filename=filename,
            row=row,
            content_hash=content_hash,
            amount_state=None,
            scope=resolved,
        )


def ingest_invoice_row(
    conn: Connection,
    *,
    filename: str,
    row: InvoiceRow,
    content_hash: str,
    amount_state: str | None = None,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    """Store a validated row in the caller's transaction and staging lock."""
    resolved = scope or ExecutionScope.local()
    invoice_ref = row.invoice_id
    snapshot = _snapshot_row(row)
    acquire_staging_lock(conn)
    extra, extra_params = _attempt_sql(resolved.attempt_id)
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
            attempt_id=resolved.attempt_id,
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
            " currency, amount_minor, amount_state)"
            " VALUES (:sid, :iid, :sup, :dt, :cur, :amt, :amt_state)"
        ),
        {
            "sid": submission_id,
            "iid": row.invoice_id,
            "sup": row.supplier_id,
            "dt": row.invoice_date,
            "cur": row.currency,
            "amt": row.amount_minor,
            "amt_state": amount_state,
        },
    )
    _replace_staging(
        conn,
        invoice_ref=invoice_ref,
        submission_id=submission_id,
        version=version,
        row=row,
        snapshot=snapshot,
        attempt_id=resolved.attempt_id,
    )
    return _submission_payload(submission_id, invoice_ref, version, False, True)


def current_staged(
    conn: Connection,
    *,
    invoice_ref: str | None = None,
    scope: ExecutionScope | None = None,
    attempt_id: str | None = None,
    lock: bool = False,
) -> RowMapping | None:
    """Return the singleton currently staged invoice row, optionally locking."""
    if lock:
        acquire_staging_lock(conn)
    effective_attempt = (
        attempt_id
        if attempt_id is not None
        else (scope.attempt_id if scope is not None else None)
    )
    query = (
        "SELECT invoice_ref, submission_id, version, snapshot FROM wb.staging_invoice"
    )
    clauses: list[str] = []
    params: dict[str, str] = {}
    if invoice_ref is not None:
        clauses.append("invoice_ref = :ref")
        params["ref"] = invoice_ref
    if effective_attempt is not None:
        clauses.append("attempt_id = :attempt")
        params["attempt"] = effective_attempt
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    return conn.execute(text(query + " LIMIT 1"), params).mappings().first()


def read_submission_history(
    conn: Connection,
    *,
    invoice_ref: str,
    scope: ExecutionScope | None = None,
    attempt_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read immutable submission versions and their stored amount state."""
    effective_attempt = (
        attempt_id
        if attempt_id is not None
        else (scope.attempt_id if scope is not None else None)
    )
    extra, extra_params = _attempt_sql(effective_attempt, alias="m")
    rows = (
        conn.execute(
            text(
                "SELECT m.version, m.id, r.invoice_id, r.supplier_id,"
                " r.invoice_date, r.currency, r.amount_minor, r.amount_state"
                " FROM wb.submissions m"
                " JOIN wb.submission_rows r ON r.submission_id = m.id"
                " WHERE m.invoice_ref = :ref" + extra + " ORDER BY m.version"
            ),
            {"ref": invoice_ref, **extra_params},
        )
        .mappings()
        .all()
    )
    return [
        {
            "version": int(h["version"]),
            "submission_id": str(h["id"]),
            "row": _row_dict(h),
            "amount_state": h["amount_state"],
        }
        for h in rows
    ]
