# LedgerLens (invoice-exception-workbench)

Operator workbench for investigating incomplete supplier invoices against the
airflow-dq-agent governance core. Milestone 1 covers one complete operator
journey with deterministic fixtures: ingest a supplier CSV, run the real core
checks against PostgreSQL, inspect the exception case, ingest a corrected
version, and resolve through an explicit gated action.

Display name: LedgerLens. Repository name: invoice-exception-workbench.

## Architecture

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

The workbench never writes to `dq.*`, never manufactures Human Decisions or
Apply Admissions, and row values never enter core audit payloads (pinned by
`tests/test_permissions.py`).

## Tested core revision

- Core checkout: `/home/youzaina001/agy-projects/projects/airflow-dq-agent/`
- Commit `0371013` (merge PR #94), package `0.2.0`
- Repro: `git -C <core> log --oneline -1` shows `0371013`; install it editable
  with `uv pip install -e <core>` inside the workbench `.venv`.
- Core settings for M1: `LLM_MODE=stub`, `APPLY_MODE=off`,
  `TRACE_POSTGRES=true` (real `dq.check_runs`/`dq.traces` rows, no approval or
  apply path).

## Case-resolution rule

A case closes only through an explicit operator Resolve action gated on a
fresh pass of the required check for the current submission version. Anything
else refuses with a machine-readable reason (`tests/test_resolution.py` pins
each one): `case-not-open`, `incomplete-report`, `check-mismatch`,
`stale-version`, `snapshot-mismatch`, `verdict-not-pass`, plus the wiring
reasons `no-verdict` and `current-missing`.

A resolved case means the configured checks passed for a specific version. It
is not payment approval. Quarantine success, rejection, and timeout are
distinct from resolution; M1 never executes those paths, so the case page
labels them Unknown instead of inferring success.

## Limits (M1)

Localhost-only synthetic demo with no login; authentication is required before
any shared deployment. One invoice per submission. Synchronous flow only.
Document AI/OCR, live Airflow HITL, quarantine execution, reconciliation, and
downstream publication are later milestones and are not scaffolded.

PostgreSQL here is real PostgreSQL 16 over TCP, managed from `pgserver`'s
bundled binaries (`src/ledgerlens/local_pg.py`) because neither Docker nor
system Postgres exists in this environment. Upgrade path: run the core
`warehouse` service (`docker compose up -d warehouse`) and point the DSNs at
it.

## Setup and demo

```bash
cd invoice-exception-workbench-m1
uv venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]" -e /path/to/airflow-dq-agent
.venv/bin/python scripts/dev_pg.py   # Postgres on 127.0.0.1:5433, writes .env
.venv/bin/uvicorn ledgerlens.app:app --host 127.0.0.1 --port 8101
```

Open `http://127.0.0.1:8101` in a browser.

Failure journey: upload `fixtures/missing_amount.csv`, run checks, open the
case (failed completeness check, full row values, Unknown audit labels).
Success journey: upload `fixtures/corrected_amount.csv` (version 2), run
checks (pass, case stays open), press Resolve (case closes). `valid_control.csv`
passes without ever opening a case.

Re-uploading an invoice's latest file restores it as the current submission
without adding history. Uploading an older duplicate leaves staging unchanged.
The browser explains both outcomes, reports refused actions, and offers Resolve
only when the current evidence permits it.

## Verification

```bash
.venv/bin/python -m pytest -q        # spins its own embedded Postgres
.venv/bin/ruff check src tests scripts && .venv/bin/ruff format --check src tests scripts
.venv/bin/mypy src tests scripts
.venv/bin/playwright install chromium # once, for tests/test_browser_journey.py
```

The suite covers HTTP journeys over real Postgres plus the installed core, pure
resolution-rule units, least-privilege and no-leak checks, and one real
Chromium journey against a live server.
