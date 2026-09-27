"""Companion-owned storage. The intake role owns wb.*.

Setup runs once with an admin DSN.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from airflow_dq_agent.adoption import login_dsn
from airflow_dq_agent.warehouse.db import make_engine
from sqlalchemy import text

INTAKE_LOGIN = "wb_intake"

TABLES_DDL = """
CREATE TABLE IF NOT EXISTS wb.submissions (
    id TEXT PRIMARY KEY,
    invoice_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (invoice_ref, version)
);
CREATE TABLE IF NOT EXISTS wb.submission_rows (
    submission_id TEXT NOT NULL REFERENCES wb.submissions (id),
    invoice_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    invoice_date DATE NOT NULL,
    currency TEXT NOT NULL,
    amount_minor BIGINT NULL
);
CREATE TABLE IF NOT EXISTS wb.staging_invoice (
    invoice_ref TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    invoice_id TEXT NOT NULL,
    supplier_id TEXT NOT NULL,
    invoice_date DATE NOT NULL,
    currency TEXT NOT NULL,
    amount_minor BIGINT NULL,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wb.cases (
    id TEXT PRIMARY KEY,
    invoice_ref TEXT NOT NULL,
    source_submission_id TEXT NOT NULL REFERENCES wb.submissions (id),
    check_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ NULL,
    resolved_run_id TEXT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS cases_one_open_per_check_idx
    ON wb.cases (invoice_ref, check_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS wb.case_verdicts (
    id BIGSERIAL PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES wb.cases (id),
    submission_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    run_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    status TEXT NOT NULL,
    audit_event_id TEXT NULL,
    n_failed INTEGER NULL,
    n_total INTEGER NULL,
    message TEXT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, check_id)
);
ALTER TABLE wb.case_verdicts
    ADD COLUMN IF NOT EXISTS sample_failures JSONB NOT NULL DEFAULT '[]'::jsonb;
GRANT USAGE ON SCHEMA wb TO dq_read;
GRANT SELECT ON wb.staging_invoice TO dq_read;
"""


@dataclass(frozen=True)
class IntakeCredentials:
    intake_dsn: str


def setup_database(admin_dsn: str) -> IntakeCredentials:
    """Create the intake role and companion tables.

    Setup-only; never called at runtime.
    """
    password = secrets.token_hex(16)
    admin = make_engine(admin_dsn)
    with admin.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :login"),
            {"login": INTAKE_LOGIN},
        ).first()
        # Quote the role name and password server-side: role DDL cannot take
        # bind parameters, so the values are never spliced in raw.
        quoted = (
            connection.execute(
                text(
                    "SELECT quote_ident(:name) AS ident,"
                    " quote_literal(:password) AS literal"
                ),
                {"name": INTAKE_LOGIN, "password": password},
            )
            .mappings()
            .one()
        )
        if exists is None:
            connection.execute(
                text(
                    f"CREATE ROLE {quoted['ident']} LOGIN"
                    f" PASSWORD {quoted['literal']} INHERIT"
                )
            )
        else:
            connection.execute(
                text(
                    f"ALTER ROLE {quoted['ident']} WITH LOGIN"
                    f" PASSWORD {quoted['literal']} INHERIT"
                )
            )
        connection.execute(
            text(f"CREATE SCHEMA IF NOT EXISTS wb AUTHORIZATION {quoted['ident']}")
        )
    intake_dsn = login_dsn(admin_dsn, user=INTAKE_LOGIN, password=password)
    with make_engine(intake_dsn).begin() as connection:
        connection.execute(text(TABLES_DDL))
    return IntakeCredentials(intake_dsn=intake_dsn)
