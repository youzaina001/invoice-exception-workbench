# Local development

[Documentation](README.md) · [Project README](../README.md)

Run all commands from the repository root. Install Python 3.12 or newer,
`uv` and Make, and obtain a checkout of the supported Airflow governance core.
PostgreSQL is supplied by `pgserver`; Chromium is required for browser tests.

## Tested core revision

- CI selects core commit `5d602428a93e5eee733b4ed1ffd06af4cbba250b` explicitly.
  The original M1 evidence used `0371013` (merge PR #94).
- Repro: check out the supported revision in your chosen core directory, then
  run `make setup CORE=/path/to/core` and `make check CORE=/path/to/core`.
  Pytest reports the interpreter, both resolved sources, and the core revision;
  Make refuses a core imported from a different directory.
- Core settings for M1: `LLM_MODE=stub`, `APPLY_MODE=off`,
  `TRACE_POSTGRES=true` (real `dq.check_runs`/`dq.traces` rows, no approval or
  apply path).

## Setup and demo

```bash
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
and `APP_PORT` are not workbench settings; `make run` sets the bind address and
port explicitly.

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

## Updating an existing installation

Reinstall from this checkout to replace the former distribution:

```bash
uv pip uninstall --python .venv/bin/python invoice-exception-workbench
uv pip install --python .venv/bin/python -e '.[dev]'
```

Update imports from `ledgerlens` to `invoice_triage` and launch commands to
`invoice_triage.app:app`. The old import name is not provided. Database schemas,
existing data, runtime settings and shared database lock identifiers keep their
existing values; renaming alone needs no database migration. The PDF review
feature adds local tables through the existing setup path. Historical verification
records retain the name and paths used when they were captured.

## Verification

```bash
make check CORE=/path/to/airflow-dq-agent  # same core selected during setup
# Or individually: make test; make lint; make typecheck
```

The suite covers HTTP journeys over real Postgres plus the installed core, pure
resolution-rule units, least-privilege and no-leak checks, and real
Chromium journeys against a live server.

If the sandbox blocks the virtualenv's bundled PEM certificate file, use the
system trust store for the test command:
`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt make check` (Linux).
This does not disable certificate verification. Tests generate isolated database
credentials and never need the demo's `.env` values.

The [M1 review follow-up](review-follow-up.md) and
[PR #1 handoff](pr1-handoff.md) are historical records. Current contributor
checks and receipt requirements are in [verification and continuation](agents/verification.md).
