# Invoice Triage

Operator workbench for investigating incomplete supplier invoices against the
airflow-dq-agent governance core. Milestone 1 covers one complete operator
journey with deterministic fixtures: ingest a supplier CSV, run the real core
checks against PostgreSQL, inspect the exception case, ingest a corrected
version, and resolve through an explicit gated action.

Display name: Invoice Triage. Python distribution: `invoice-triage`.
Import package: `invoice_triage`. Repository: `invoice-exception-workbench`.

### Updating an existing installation

Reinstall from this checkout to replace the former distribution:

```bash
uv pip uninstall --python .venv/bin/python invoice-exception-workbench
uv pip install --python .venv/bin/python -e '.[dev]'
```

Update imports from `ledgerlens` to `invoice_triage` and launch commands to
`invoice_triage.app:app`. The old import name is not provided. Database schemas,
existing data, runtime settings and shared database lock identifiers keep their
existing values; no database migration is needed. Historical verification
records retain the name and paths used when they were captured.

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

The workbench has no direct table writes to `dq.*`, never manufactures Human Decisions or
Apply Admissions, and row values never enter core audit payloads (pinned by
`tests/test_permissions.py`).

## Tested core revision

- CI selects core commit `a1477241fa856df64687e998347e3ecca315619a` explicitly.
  The original M1 evidence used `0371013` (merge PR #94).
- Repro: check out the supported revision in your chosen core directory, then
  run `make setup CORE=/path/to/core` and `make check CORE=/path/to/core`.
  Pytest reports the interpreter, both resolved sources, and the core revision;
  Make refuses a core imported from a different directory.
- Core settings for M1: `LLM_MODE=stub`, `APPLY_MODE=off`,
  `TRACE_POSTGRES=true` (real `dq.check_runs`/`dq.traces` rows, no approval or
  apply path).

## Case-resolution rule

A case closes only through an explicit operator Resolve action gated on a
fresh pass of the required check for the current submission version. Anything
else refuses with a machine-readable reason (`tests/test_resolution.py` pins
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

PostgreSQL here is real PostgreSQL 16 over TCP, managed from `pgserver`'s
bundled binaries (`src/invoice_triage/local_pg.py`) because neither Docker nor
system Postgres exists in this environment. Upgrade path: run the core
`warehouse` service (`docker compose up -d warehouse`) and point the DSNs at
it.

## Setup and demo

```bash
cd invoice-exception-workbench
make setup CORE=/path/to/airflow-dq-agent  # use the supported core revision above
make browser-install                    # Chromium for the browser test
make db                                 # keep running in terminal 1
# In terminal 2, from this same directory:
make run                                # 127.0.0.1:8101; optional PORT=8102
```

Open `http://127.0.0.1:8101` in a browser. `make db` creates a password-protected
local PostgreSQL cluster and writes generated role credentials to the ignored
runtime `.env`. Keep the setup-only `.env.setup` separate. Existing clusters
created before password authentication must be replaced with a fresh demo
cluster; preserve any evidence you need before doing so.

`make run` fixes the bind address to loopback. `create_app()` is an ASGI factory
and does not choose a network interface; custom server commands must retain the
localhost bind. Runtime settings contain only intake, read, and audit DSNs.

### Environment example (placeholders, no secrets)

For an existing PostgreSQL instance, provision the three roles first and set:

```dotenv
INTAKE_DSN=postgresql+psycopg://wb_intake:REPLACE_WITH_GENERATED_PASSWORD@127.0.0.1:5433/warehouse
READ_DSN=postgresql+psycopg://dq_read_login:REPLACE_WITH_GENERATED_PASSWORD@127.0.0.1:5433/warehouse
AUDIT_DSN=postgresql+psycopg://dq_audit_login:REPLACE_WITH_GENERATED_PASSWORD@127.0.0.1:5433/warehouse
LLM_MODE=stub
APPLY_MODE=off
TRACE_POSTGRES=true
TRACES_DIR=traces
```

No admin or apply DSN is required by the runtime. `WAREHOUSE_DSN`, `APP_HOST`,
and `APP_PORT` are not workbench settings. The generated `.env` may retain these
legacy keys; `make run` sets the bind address and port explicitly.

Failure journey: upload `fixtures/missing_amount.csv`, run checks, open the
case (failed completeness check, full row values, Unknown labels for absent
remediation events).
Success journey: upload `fixtures/corrected_amount.csv` (version 2), run
checks (pass, case stays open), press Resolve (case closes). `valid_control.csv`
passes without ever opening a case.

Re-uploading an invoice's latest file restores it as the current submission
without adding history. Uploading an older duplicate leaves staging unchanged.
The browser explains both outcomes, reports refused actions, and offers Resolve
only when the current evidence permits it.

## Verification

```bash
make check CORE=/path/to/airflow-dq-agent  # same core selected during setup
# Or individually: make test; make lint; make typecheck
```

The suite covers HTTP journeys over real Postgres plus the installed core, pure
resolution-rule units, least-privilege and no-leak checks, and one real
Chromium journey against a live server.


If the sandbox blocks the virtualenv's bundled PEM certificate file, use the
system trust store for the test command:
`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt make check` (Linux).
This does not disable certificate verification. Tests generate isolated database
credentials and never need the demo's `.env` values.

See [review follow-up](docs/review-follow-up.md) for finding dispositions and
[PR handoff](docs/pr1-handoff.md) for verification and publishing status.

## Hosted invitation-only demo

### Prepare the database locally as its owner

Use a dedicated Neon database for synthetic demonstrations, with the free-plan
and no-payment-method choices recorded in the hosting ADR. Install this checkout
and its matching governance core in the same Python 3.12 environment using the
setup instructions above. From the workbench directory, run the existing setup
helpers once with the owner's connection string (including its TLS options):

```bash
.venv/bin/python - <<'PY'
from getpass import getpass
from sqlalchemy.engine import make_url
from airflow_dq_agent.adoption import apply_governance_schema, provision_restricted_logins
from invoice_triage.db import setup_database

owner = make_url(getpass("Neon owner connection string: ")).set(
    drivername="postgresql+psycopg"
).render_as_string(hide_password=False)
apply_governance_schema(owner)
core = provision_restricted_logins(owner)
intake = setup_database(owner)
print(f"INTAKE_DSN={intake.intake_dsn}")
print(f"READ_DSN={core.read_dsn}")
print(f"AUDIT_DSN={core.audit_dsn}")
PY
```

This reuses the provisioning order in `scripts/dev_pg.py` without starting its
local PostgreSQL cluster or writing runtime files. It installs the workbench
tables, scoped policies, and audit-retention function; it does not seed invoices.
Copy the three generated values from your local terminal into the web service's
private environment settings. Keep the owner connection string out of the
runtime. The core helper also provisions an apply login, which Invoice Triage does
not use. Re-running provisioning rotates passwords: replace the runtime values
and restart the service afterward.

### Configure and start the operator service

Local mode remains unsigned (`HOSTED=false`). Hosted mode requires a verified
Clerk session before every operator action. Set `HOSTED=true`, the existing
restricted `INTAKE_DSN`, `READ_DSN`, `AUDIT_DSN`, and these public Clerk settings:

- `CLERK_ISSUER`: your exact Clerk Frontend API origin (HTTPS, no trailing slash).
- `CLERK_JWT_KEY`: that instance's complete JWT **public** key, with real newlines.
- `CLERK_AUTHORIZED_PARTIES`: JSON list of exact allowed application origins,
  including scheme and port, such as `["https://your-app.onrender.com"]`.
- `CLERK_PUBLISHABLE_KEY`: that instance's public `pk_...` key for ClerkJS.

Paste the complete public key into the environment value as multiple lines,
including its `BEGIN PUBLIC KEY` and `END PUBLIC KEY` markers. Do not surround
the value with shell quotes or replace the line breaks with literal `\n` text.
All four Clerk settings must refer to the same instance. Set the exact Render
application origin in `CLERK_AUTHORIZED_PARTIES` and the instance's allowed
application origins; do not substitute the Clerk issuer for the application origin.

Also set these core runtime values explicitly:

```text
HOSTED=true
LLM_MODE=stub
APPLY_MODE=off
TRACE_POSTGRES=true
TRACES_DIR=/tmp/invoice_triage-traces
```

PostgreSQL holds the durable evidence; local JSONL traces are supplementary and
may disappear on Render's ephemeral disk. No owner, apply, or model credential
is needed by the service. Install both this package and the reviewed core
revision in the Render build environment; the core is not bundled in this
repository. For example, replace the revision placeholder in this build command
with the core commit you validated alongside the workbench:

```bash
python -m pip install . 'git+https://github.com/youzaina001/airflow-dq-agent.git@<reviewed-core-commit>'
```

Use this Render start command from the installed workbench directory:

```bash
uvicorn invoice_triage.app:app --host 0.0.0.0 --port "$PORT"
```

Render supplies `PORT`. The local `make run` command deliberately binds to
loopback and is not the hosted start command. Select the existing ADR's free
service settings; this documentation does not create or deploy a service.

In Clerk's dashboard, select **Access mode → Invite-only → Save**, following
[Clerk's access-mode guide](https://clerk.com/docs/guides/secure/restricting-access).
That mode hides public sign-up in the sign-in component. The Maintainer invites Reviewers in Clerk;
Invoice Triage has no invitation or public registration screen. The sign-in page
loads ClerkJS and disables its sign-up option. Clerk refreshes the browser's
`__session` cookie; the server independently verifies RS256 signatures, issuer,
expiry, not-before, subject, session and authorized-party claims on every request.
Pending sessions are refused. JWT public keys must be updated if Clerk rotates
the instance key. No Clerk secret key, provider API call, or shared demo password
is used for server verification. Missing configuration fails closed.

Cookie-authenticated mutations also require an `Origin` in
`CLERK_AUTHORIZED_PARTIES`; missing or foreign origins are refused. API clients
may send `Authorization: Bearer <session-token>` instead. `X-Reviewer` has no
runtime authority; tests explicitly inject the verified-identity boundary.
See [Clerk's verification guide](https://clerk.com/docs/guides/sessions/manual-jwt-verification)
and [Clerk's sign-in component](https://clerk.com/docs/js-frontend/reference/components/authentication/sign-in).

Before sharing a deployment, manually check the configured provider UI: an
unsigned visit shows sign-in without public sign-up; an invited Reviewer can
sign in, upload the synthetic missing/corrected samples, run checks, and resolve
the case; revisiting resumes that work; Start fresh exposes read-only history;
and a second invited Reviewer cannot see the first one's cases or attempts.
Sign out and confirm operator pages are refused again. The automated tests use
local signed session fixtures, not a live Clerk instance. These provider checks
remain unverified until performed against the configured service.

After sign-in the browser exposes **Start fresh**, the current empty attempt,
and links to previous read-only attempts with submissions, cases, check results,
and audit evidence. Each Reviewer retains at most five attempts. Initialization,
Start fresh and each mutation use the same PostgreSQL per-Reviewer transaction
lock. Mutations revalidate their selected attempt while holding it, so an upload,
check or resolution that resumes after a transition refuses with HTTP 409.
The existing staging lock still serializes check execution across Reviewers.

Audit retention is a narrow exception to the normal append-only permissions:
`wb_intake` may execute owner-owned `wb.delete_attempt_audit(text)`, a
`SECURITY DEFINER` function with fixed search path and no PUBLIC execute grant.
It locks the Reviewer's workspace and permits only that Reviewer's oldest attempt
when more than five exist, within the Start fresh transaction. It refuses current,
retained and foreign attempts. The same transaction removes the attempt's intake
rows. Runtime roles receive no direct UPDATE/DELETE grant on audit tables;
`dq_audit` remains append-only. The database owner can inspect every workspace
and retained historical attempt for maintenance. No hosted deployment is claimed.

See [verification and continuation](docs/agents/verification.md) for source checks
and retained receipts. [Domain terms](CONTEXT.md) and the
[accepted hosted-services plan](docs/adr/0001-select-hosted-services.md) name the
hosted demonstration. No hosted deployment is claimed.
