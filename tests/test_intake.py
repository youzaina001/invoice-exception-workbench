"""Deepened submission intake, strict amount parsing, and singleton staging tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from invoice_triage.intake import (
    InvoiceRow,
    current_staged,
    ingest_invoice_row,
    ingest_submission,
    parse_amount_minor,
    read_submission_history,
)
from invoice_triage.request_scope import (
    AttemptReadOnlyError,
    ExecutionScope,
    install_scope,
    scoped_transaction,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _scoped_engine(dsn: str) -> Engine:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    install_scope(engine)
    return engine


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("0", 0),
        ("000.0", 0),
        ("12", 1200),
        ("12.3", 1230),
        ("12.34", 1234),
        ("92233720368547758.07", 9_223_372_036_854_775_807),
    ],
)
def test_parse_amount_minor_strict_unsigned_decimal_dot(
    raw: str, expected: int | None
) -> None:
    assert parse_amount_minor(raw, filename="sample.csv") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1e2",
        "+12.34",
        "-1.00",
        "1.000",
        "0.001",
        "1,234.56",
        "NaN",
        "Infinity",
        "1e999999",
        "1e1000000",
        "92233720368547758.08",
        "9" * 5000,
        "12.",
        ".50",
        "１２.３４",
        "abc",
    ],
)
def test_parse_amount_minor_rejects_non_decimal_dot_inputs(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_amount_minor(raw, filename="bad.csv")


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        (name, (FIXTURES / name).read_bytes())
        for name in (
            "bad_header.csv",
            "bad_amount.csv",
            "two_rows.csv",
            "subcent_amount.csv",
            "long_subcent_amount.csv",
            "negative_amount.csv",
            "extra_field.csv",
            "extra_empty_field.csv",
            "infinite_amount.csv",
            "huge_amount.csv",
        )
    ]
    + [
        (
            f"inline_{label}.csv",
            (
                "invoice_id,supplier_id,invoice_date,currency,amount\n"
                f"INV-BAD,SUP-1,2026-09-01,EUR,{raw}\n"
            ).encode(),
        )
        for label, raw in (
            ("sci", "1e2"),
            ("plus", "+12.34"),
            ("exp_small", "1e999999"),
            ("exp_large", "1e1000000"),
        )
    ],
)
def test_ingest_submission_rejects_malformed_csv_without_persisting(
    pg_dsns: dict[str, str], filename: str, payload: bytes
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        with pytest.raises(ValueError):
            ingest_submission(engine, filename=filename, data=payload)
        with engine.connect() as conn:
            assert current_staged(conn) is None
            assert (
                conn.execute(text("SELECT COUNT(*) FROM wb.submissions")).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_ingest_submission_refuses_read_only_attempt(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        scope = ExecutionScope(
            hosted=True,
            reviewer_id="rev-1",
            workspace_id="ws-1",
            attempt_id="att-2",
            requested_attempt_id="att-1",
        )
        with pytest.raises(AttemptReadOnlyError, match="attempt-read-only"):
            ingest_submission(
                engine,
                filename="missing_amount.csv",
                data=(FIXTURES / "missing_amount.csv").read_bytes(),
                scope=scope,
            )
        with engine.connect() as conn:
            assert current_staged(conn) is None
            assert (
                conn.execute(text("SELECT COUNT(*) FROM wb.submissions")).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_ingest_invoice_row_persists_amount_state_and_history_without_pdf_tables(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        scope = ExecutionScope.local()
        row = InvoiceRow("INV-PDF", "SUP-1", date(2026, 9, 1), "EUR", None)
        with scoped_transaction(engine, scope, mutate=True, lock_staging=True) as conn:
            created = ingest_invoice_row(
                conn,
                filename="review.pdf",
                row=row,
                content_hash="a" * 64,
                amount_state="ambiguous",
                scope=scope,
            )
        assert created["invoice_ref"] == "INV-PDF"
        assert created["version"] == 1
        assert created["duplicate"] is False
        assert created["made_current"] is True

        with engine.connect() as conn:
            staged = current_staged(conn, invoice_ref="INV-PDF")
            assert staged is not None
            assert staged["version"] == 1
            history = read_submission_history(conn, invoice_ref="INV-PDF")
            assert len(history) == 1
            assert history[0]["version"] == 1
            assert history[0]["submission_id"] == created["submission_id"]
            assert history[0]["amount_state"] == "ambiguous"
            assert history[0]["row"]["amount_minor"] is None
            assert (
                conn.execute(text("SELECT COUNT(*) FROM wb.pdf_intakes")).scalar_one()
                == 0
            )
            assert (
                conn.execute(text("SELECT COUNT(*) FROM wb.pdf_revisions")).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_ingest_submission_restages_latest_duplicate_and_ignores_older_duplicate(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    missing = (FIXTURES / "missing_amount.csv").read_bytes()
    corrected = (FIXTURES / "corrected_amount.csv").read_bytes()
    control = (FIXTURES / "valid_control.csv").read_bytes()
    try:
        first = ingest_submission(engine, filename="missing_amount.csv", data=missing)
        second = ingest_submission(
            engine, filename="corrected_amount.csv", data=corrected
        )
        assert (first["version"], second["version"]) == (1, 2)

        older_dup = ingest_submission(
            engine, filename="missing_amount.csv", data=missing
        )
        assert (
            older_dup["duplicate"],
            older_dup["version"],
            older_dup["made_current"],
        ) == (True, 1, False)
        with engine.connect() as conn:
            staged = current_staged(conn)
            assert staged is not None
            assert (staged["invoice_ref"], staged["version"]) == ("INV-001", 2)

        ingest_submission(engine, filename="valid_control.csv", data=control)
        with engine.connect() as conn:
            staged = current_staged(conn)
            assert staged is not None
            assert (staged["invoice_ref"], staged["version"]) == ("INV-002", 1)

        latest_dup = ingest_submission(
            engine, filename="corrected_amount.csv", data=corrected
        )
        assert (
            latest_dup["duplicate"],
            latest_dup["version"],
            latest_dup["made_current"],
        ) == (True, 2, True)
        with engine.connect() as conn:
            staged = current_staged(conn)
            assert staged is not None
            assert (staged["invoice_ref"], staged["version"]) == ("INV-001", 2)
    finally:
        engine.dispose()
