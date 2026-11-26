# LedgerLens architecture and workflow boundaries

[Documentation](README.md) · [Project README](../README.md)

LedgerLens is the portfolio name for Invoice Triage, the workbench in
`invoice-exception-workbench`. CI pins the Airflow governance core to
`5d602428a93e5eee733b4ed1ffd06af4cbba250b`. See
[local development](local-development.md#tested-core-revision) for setup.

```text
CSV or confirmed local PDF -> wb.submissions (immutable history, intake role)
                           -> wb.staging_invoice (current submission)
                           -> core run_quality_suite (read login)
                           -> dq.check_runs + dq.traces (audit login, sample-free)
                           -> wb.cases + wb.case_verdicts (intake role)
                           -> operator pages (FastAPI + Jinja)
```

One synchronous web process calls the installed core in-process. No broker, no
background workers, no separate frontend build. Money stays in integer minor
units (`amount_minor`); the core float demo is never used for business values.

Every verdict records the invoice ref, submission version, staging
snapshot hash, run id, report id, check id, and status. Resolution compares the
latest verdict against the current staging row, so stale results can never
close a case. A PostgreSQL transaction-level advisory lock serializes staging
changes, checks, and resolution across Reviewers. Concurrent check requests
retain each verdict on one open case.

## Database permissions

| Login | May access |
| --- | --- |
| `wb_intake` | Owns `wb.*` under forced row-level security. No direct access to `dq.*` tables. |
| `dq_read_login` | `SELECT` on `wb.staging_invoice` only; no writes anywhere |
| `dq_audit_login` | Append/read `dq.traces`, append `dq.check_runs`; no `wb.*` access |
| Database owner | Setup and maintenance of schemas, roles, and grants. |

The core writes audit evidence via the audit login. Row values stay out of core
audit payloads, as checked by [the permissions tests](../tests/test_permissions.py).

## Runtime modes and row-level security

- **Local (`HOSTED=false`)** runs on loopback without sign-in. The Operator can
  upload synthetic CSVs or [review and confirm a PDF](pdf-review.md) before
  submission. Local transactions leave the Reviewer and Demo Attempt settings unset.
- **Hosted (`HOSTED=true`)** verifies a Clerk session JWT for each Operator
  request. The [invitation-only setup](hosted-demo.md) uses synthetic CSVs and
  disables PDF intake. Each invited Reviewer gets a private Demo Workspace.
  **Start fresh** opens a new Demo Attempt and leaves earlier attempts read-only.
  The workspace retains the current attempt and up to four previous attempts.

PostgreSQL policies enforce Reviewer isolation on workbench and audit rows.
Provisioning applies `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`
on the scoped `wb.*` tables, and enables row-level security on `dq.traces` and
`dq.check_runs` for the restricted audit role. Hosted transactions bind
`app.reviewer` from the verified session. The active attempt is bound through
`app.attempt` during mutations and checks, so the core sees only that
attempt's staging row. Transaction-local settings keep pooled connections from
carrying the scope to the next request.

Hosted mutations revalidate the current attempt under a per-Reviewer transaction
lock. When **Start fresh** exceeds the retention cap, the intake role invokes
`wb.delete_attempt_audit` to remove only the oldest attempt's audit rows within
the same transaction. The runtime roles have no direct update or delete grants
on the audit tables.

## Case-resolution rule

A case closes only through the Operator's explicit Resolve action gated on a
fresh pass of the required check and complete audit evidence for the current
submission version. The gate returns ten machine-readable refusal reasons,
covered by [the case lifecycle tests](../tests/test_case_lifecycle.py).
They are `case-not-open`, `attempt-read-only`, `no-verdict`, `current-missing`,
`errored-report`, `incomplete-report`, `check-mismatch`, `stale-version`,
`snapshot-mismatch`, and `verdict-not-pass`.

A resolved case means the configured checks passed for a specific version. It
is not payment approval. Quarantine success, rejection, and timeout are
distinct from resolution. The case page reads stored core events for its checked
runs; missing or unreadable evidence displays as `Unknown`. Labels describe the
latest checked run; the audit history retains events from earlier versions.
An approval review displays “Review recorded (mode Unknown)” because the stored
event omits the execution mode. A quarantine copy is reported
only for a successful `quarantine_nulls` step with a positive row count on the
staging table. None of these events closes a case or authorizes payment.
The workbench reads these events; it never executes the Airflow approval/apply path.

## Runtime limits

Each submission covers one invoice. Local mode has one current staging row, and
hosted mode has one per Demo Attempt. Submitting a different invoice replaces
the current row in that scope. The displaced invoice's open case retains its
history and requires restaging and fresh checks before resolution.

CSV and PDF uploads are limited to 5 MiB. Invalid amounts, including sub-cent
and out-of-range values, are rejected before submission. Local PDF review uses
embedded text and retains the original file and review history in the local
database. Scanned PDFs require OCR, which is outside the workbench.

The local demo uses real PostgreSQL 16 over TCP, managed from `pgserver`'s
bundled binaries (`src/invoice_triage/local_pg.py`). An existing PostgreSQL
instance can instead provide the restricted DSNs described in
[local setup](local-development.md#environment-example-placeholders-no-secrets).
