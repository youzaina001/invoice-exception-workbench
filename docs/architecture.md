# Architecture and workflow boundaries

[Documentation](README.md) · [Project README](../README.md)

```text
CSV ingest -> wb.submissions (immutable history, intake role)
           -> wb.staging_invoice (current projection only, intake writes)
           -> core run_quality_suite via read login (SELECT staging only)
           -> dq.check_runs + dq.traces via audit login (sample-free)
           -> wb.cases + wb.case_verdicts (binding + resolution gate)
           -> operator pages (FastAPI + Jinja, localhost)
```

One synchronous web process calls the installed core in-process. No broker, no
background workers, no separate frontend build. Money stays in integer minor
units (`amount_minor`); the core float demo is never used for business values.

Binding: every verdict records invoice ref, submission version, staging
snapshot hash, run id, report id, check id, and status. Resolution compares the
latest verdict against the current staging row, so stale results can never
close a case. Uploads, checks, and resolution share a PostgreSQL transaction
lock across the single staging projection; concurrent check requests retain
each verdict on one open case.

Permissions:

| Login | May access |
| --- | --- |
| `wb_intake` | Owns `wb.*`; no access to `dq.*` |
| `dq_read_login` | `SELECT` on `wb.staging_invoice` only; no writes anywhere |
| `dq_audit_login` | Append/read `dq.traces`, append `dq.check_runs`; no `wb.*` access |
| admin (`postgres`/`dq`) | Setup only: schema, roles, grants. Never a runtime credential. |

The workbench has no direct table writes to `dq.*`, never manufactures Human Decisions or
Apply Admissions, and row values never enter core audit payloads (pinned by
`tests/test_permissions.py`).

## Case-resolution rule

A case closes only through an explicit operator Resolve action gated on a
fresh pass of the required check for the current submission version. Anything
else refuses with a machine-readable reason (`tests/test_case_lifecycle.py` pins
each one): `case-not-open`, `errored-report`, `incomplete-report`, `check-mismatch`,
`stale-version`, `snapshot-mismatch`, `verdict-not-pass`, plus the wiring
reasons `no-verdict` and `current-missing`.

A resolved case means the configured checks passed for a specific version. It
is not payment approval. Quarantine success, rejection, and timeout are
distinct from resolution. The case page reads stored core events for its checked
runs; missing or unreadable evidence displays as `Unknown`. Labels describe the
latest checked run; the audit history retains events from earlier versions.
An approval review does not identify shadow versus HITL mode in core `0371013`,
so it displays “Review recorded (mode Unknown)”. A quarantine copy is reported
only for a successful `quarantine_nulls` step with a positive row count on the
staging table. None of these events closes a case or authorizes payment.
The workbench reads these events; it never executes the Airflow approval/apply path.

## Limits (M1)

Localhost-only synthetic demo with no login; authentication is required before
any shared deployment. One invoice per submission and one current staging row
across all invoices. Uploading another invoice displaces the current row; its
open case retains history but cannot resolve until its latest CSV is restaged
and checked. This prevents the core table-wide check from mixing invoices.
Synchronous flow only. Uploads are limited to 5 MiB per CSV; invalid amounts,
including sub-cent and out-of-range values, are rejected before storage.
Document AI/OCR, live Airflow HITL, quarantine execution, reconciliation, and
downstream publication are later milestones and are not scaffolded.

The local demo uses real PostgreSQL 16 over TCP, managed from `pgserver`'s
bundled binaries (`src/invoice_triage/local_pg.py`). An existing PostgreSQL
instance can instead provide the restricted DSNs described in
[local setup](local-development.md#environment-example-placeholders-no-secrets).
