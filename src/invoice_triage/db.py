"""Companion-owned storage. The intake role owns wb.*.

Setup runs once with an admin DSN.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

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
CREATE TABLE IF NOT EXISTS wb.demo_workspaces (
    id TEXT PRIMARY KEY,
    reviewer_id TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS wb.demo_attempts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES wb.demo_workspaces (id),
    reviewer_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
GRANT USAGE ON SCHEMA wb TO dq_read;
GRANT SELECT ON wb.staging_invoice TO dq_read;
ALTER TABLE wb.submissions
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE wb.submission_rows
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE wb.staging_invoice
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE wb.cases
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE wb.case_verdicts
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
"""

# One current staging row per Demo Attempt, and the same invoice reference may
# exist in another attempt. Local rows leave attempt_id null and keep the
# historical one-row-per-reference rule.
LOCAL_PDF_DDL = """
CREATE TABLE IF NOT EXISTS wb.pdf_sources (
    id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL UNIQUE,
    data BYTEA NOT NULL,
    filename TEXT NOT NULL,
    pages JSONB NOT NULL,
    raw JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS wb.pdf_drafts (
    id TEXT PRIMARY KEY,
    owner_context TEXT NOT NULL CHECK (owner_context = 'local'),
    revision INTEGER NOT NULL CHECK (revision > 0)
);
CREATE TABLE IF NOT EXISTS wb.pdf_revisions (
    draft_id TEXT NOT NULL REFERENCES wb.pdf_drafts(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    source_id TEXT NOT NULL REFERENCES wb.pdf_sources(id),
    fields JSONB,
    binding TEXT NOT NULL,
    PRIMARY KEY (draft_id, revision)
);
CREATE TABLE IF NOT EXISTS wb.pdf_confirmations (
    draft_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    binding TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (draft_id, revision),
    FOREIGN KEY (draft_id, revision)
        REFERENCES wb.pdf_revisions(draft_id, revision)
);
ALTER TABLE wb.pdf_revisions ADD COLUMN IF NOT EXISTS extraction JSONB;
CREATE TABLE IF NOT EXISTS wb.pdf_intakes (
    draft_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    submission_id TEXT NOT NULL REFERENCES wb.submissions(id),
    PRIMARY KEY (draft_id, revision),
    FOREIGN KEY (draft_id, revision)
        REFERENCES wb.pdf_confirmations(draft_id, revision)
);
DO $$
DECLARE target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'pdf_sources', 'pdf_drafts', 'pdf_revisions',
        'pdf_confirmations', 'pdf_intakes'
    ] LOOP
        EXECUTE format('ALTER TABLE wb.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE wb.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS local_pdf ON wb.%I', target);
        EXECUTE format(
            'CREATE POLICY local_pdf ON wb.%I USING ('
            'current_setting(''app.hosted'', true) IS DISTINCT FROM ''true'')',
            target
        );
    END LOOP;
END $$;
"""

SCOPED_DDL = """
ALTER TABLE wb.submissions
    DROP CONSTRAINT IF EXISTS submissions_invoice_ref_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS submissions_scope_invoice_version_idx
    ON wb.submissions (COALESCE(attempt_id, ''), invoice_ref, version);
DROP INDEX IF EXISTS wb.cases_one_open_per_check_idx;
CREATE UNIQUE INDEX cases_one_open_per_check_idx
    ON wb.cases (COALESCE(attempt_id, ''), invoice_ref, check_id)
    WHERE status = 'open';
DO $$
DECLARE
    pk_name name;
BEGIN
    SELECT con.conname INTO pk_name
    FROM pg_constraint con
    WHERE con.conrelid = 'wb.staging_invoice'::regclass
      AND con.contype = 'p';
    IF pk_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE wb.staging_invoice DROP CONSTRAINT %I', pk_name
        );
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS staging_local_invoice_ref_idx
    ON wb.staging_invoice (invoice_ref) WHERE attempt_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS staging_one_row_per_attempt_idx
    ON wb.staging_invoice (attempt_id) WHERE attempt_id IS NOT NULL;
CREATE OR REPLACE FUNCTION wb.stamp_hosted_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reviewer_id IS NULL THEN
        NEW.reviewer_id := NULLIF(current_setting('app.reviewer', true), '');
    END IF;
    IF NEW.attempt_id IS NULL THEN
        NEW.attempt_id := NULLIF(current_setting('app.attempt', true), '');
    END IF;
    RETURN NEW;
END;
$$;
CREATE OR REPLACE FUNCTION wb.stamp_reviewer()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reviewer_id IS NULL THEN
        NEW.reviewer_id := NULLIF(current_setting('app.reviewer', true), '');
    END IF;
    RETURN NEW;
END;
$$;
DO $$
DECLARE
    target text;
    stamp text;
    using_expr text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'submissions',
        'submission_rows',
        'staging_invoice',
        'cases',
        'case_verdicts',
        'demo_workspaces',
        'demo_attempts'
    ]
    LOOP
        IF target IN ('demo_workspaces', 'demo_attempts') THEN
            stamp := 'wb.stamp_reviewer()';
        ELSE
            stamp := 'wb.stamp_hosted_scope()';
        END IF;
        IF target = 'staging_invoice' THEN
            -- The quality suite reads every visible staging row. When this
            -- request names a Demo Attempt, hide the other attempts' rows.
            using_expr := $policy$
                (reviewer_id IS NULL
                AND current_setting('app.hosted', true) IS DISTINCT FROM 'true')
                OR (reviewer_id = current_setting('app.reviewer', true)
                AND (NULLIF(current_setting('app.attempt', true), '') IS NULL
                OR attempt_id = NULLIF(current_setting('app.attempt', true), '')))
            $policy$;
        ELSE
            using_expr :=
                '(reviewer_id IS NULL AND current_setting(''app.hosted'', true) '
                'IS DISTINCT FROM ''true'') '
                'OR reviewer_id = current_setting(''app.reviewer'', true)';
        END IF;
        EXECUTE format(
            'DROP TRIGGER IF EXISTS stamp_hosted_scope ON wb.%I', target
        );
        EXECUTE format(
            'CREATE TRIGGER stamp_hosted_scope BEFORE INSERT ON wb.%I '
            'FOR EACH ROW EXECUTE FUNCTION %s',
            target,
            stamp
        );
        EXECUTE format(
            'ALTER TABLE wb.%I ENABLE ROW LEVEL SECURITY', target
        );
        EXECUTE format(
            'ALTER TABLE wb.%I FORCE ROW LEVEL SECURITY', target
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS hosted_reviewer ON wb.%I', target
        );
        EXECUTE format(
            'CREATE POLICY hosted_reviewer ON wb.%I USING (%s)',
            target,
            using_expr
        );
    END LOOP;
END $$;
"""

AUDIT_SCOPE_DDL = """
ALTER TABLE dq.traces
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
ALTER TABLE dq.check_runs
    ADD COLUMN IF NOT EXISTS reviewer_id TEXT,
    ADD COLUMN IF NOT EXISTS attempt_id TEXT;
CREATE OR REPLACE FUNCTION dq.stamp_hosted_scope()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reviewer_id IS NULL THEN
        NEW.reviewer_id := NULLIF(current_setting('app.reviewer', true), '');
    END IF;
    IF NEW.attempt_id IS NULL THEN
        NEW.attempt_id := NULLIF(current_setting('app.attempt', true), '');
    END IF;
    RETURN NEW;
END;
$$;
GRANT EXECUTE ON FUNCTION dq.stamp_hosted_scope() TO dq_audit;
DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY['traces', 'check_runs']
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS stamp_hosted_scope ON dq.%I', target
        );
        EXECUTE format(
            'CREATE TRIGGER stamp_hosted_scope BEFORE INSERT ON dq.%I '
            'FOR EACH ROW EXECUTE FUNCTION dq.stamp_hosted_scope()',
            target
        );
        EXECUTE format('ALTER TABLE dq.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format(
            'DROP POLICY IF EXISTS hosted_reviewer ON dq.%I', target
        );
        EXECUTE format(
            'CREATE POLICY hosted_reviewer ON dq.%I USING ('
            '(reviewer_id IS NULL AND current_setting(''app.hosted'', true) '
            'IS DISTINCT FROM ''true'') '
            'OR reviewer_id = current_setting(''app.reviewer'', true))',
            target
        );
    END LOOP;
END $$;
CREATE OR REPLACE FUNCTION wb.delete_attempt_audit(target_attempt text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, wb, dq
AS $fn$
DECLARE
    reviewer text := NULLIF(current_setting('app.reviewer', true), '');
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('ledgerlens:workspace:' || reviewer, 0));
    IF reviewer IS NULL
       OR (SELECT count(*) FROM wb.demo_attempts WHERE reviewer_id = reviewer) <= 5
       OR target_attempt IS DISTINCT FROM (
           SELECT id FROM wb.demo_attempts WHERE reviewer_id = reviewer
           ORDER BY created_at ASC, id ASC LIMIT 1
       ) THEN
        RAISE EXCEPTION 'retention not permitted';
    END IF;
    DELETE FROM dq.check_runs
    WHERE attempt_id = target_attempt AND reviewer_id = reviewer;
    DELETE FROM dq.traces
    WHERE attempt_id = target_attempt AND reviewer_id = reviewer;
END;
$fn$;
REVOKE ALL ON FUNCTION wb.delete_attempt_audit(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION wb.delete_attempt_audit(text) TO wb_intake;
"""


@dataclass(frozen=True)
class IntakeCredentials:
    intake_dsn: str


def _make_engine(dsn: str) -> Engine:
    return create_engine(dsn, future=True, pool_pre_ping=True)


def _login_dsn(admin_dsn: str, *, user: str, password: str) -> str:
    return (
        make_url(admin_dsn)
        .set(username=user, password=password)
        .render_as_string(hide_password=False)
    )


def setup_database(admin_dsn: str) -> IntakeCredentials:
    """Create the intake role and companion tables.

    Setup-only; never called at runtime.
    """
    password = secrets.token_hex(16)
    admin = _make_engine(admin_dsn)
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
    intake_dsn = _login_dsn(admin_dsn, user=INTAKE_LOGIN, password=password)
    with _make_engine(intake_dsn).begin() as connection:
        connection.execute(text(TABLES_DDL))
        connection.execute(text(SCOPED_DDL))
        connection.execute(text(LOCAL_PDF_DDL))
    with admin.begin() as connection:
        connection.execute(text(AUDIT_SCOPE_DDL))
    return IntakeCredentials(intake_dsn=intake_dsn)
