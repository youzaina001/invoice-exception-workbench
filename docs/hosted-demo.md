# Hosted invitation-only demo

[Documentation](README.md) · [Project README](../README.md)

These are preparation instructions for the accepted hosting design, not proof
of a deployed service. PDF intake remains disabled in hosted mode. Recheck the
providers' current availability and budget limits before provisioning. Commands
run from the repository root.

## Prepare the database locally as its owner

Use a dedicated Neon database for synthetic demonstrations, with the free-plan
and no-payment-method choices recorded in the hosting ADR. Install this checkout
and its matching governance core in the same Python 3.12 environment using the
[local setup instructions](local-development.md). From the workbench directory, run the existing setup
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

## Configure and start the operator service

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

See [verification](local-development.md#verification) for source checks.
[Domain terms](../CONTEXT.md) and the
[accepted hosted-services plan](adr/0001-select-hosted-services.md) name the
hosted demonstration. No hosted deployment is claimed.
