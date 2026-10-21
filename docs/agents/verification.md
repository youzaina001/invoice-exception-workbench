# Verification and continuation

Create a separate environment with `make setup CORE=/path/to/airflow-dq-agent`
from this task checkout, then `make browser-install`. Never copy an editable
environment from another worktree. `make check CORE=/path/to/airflow-dq-agent`
runs the PostgreSQL and Chromium journeys, lint/format checks, and strict typing.
The core source selected for setup must also be selected for checks.

Pytest rejects a product package imported from another checkout before collecting
tests. Its first output line identifies the interpreter, Invoice Triage source,
Airflow core source, and core Git revision (or `unavailable` for an installation
without Git metadata). Make additionally requires the selected `CORE` source.
Direct pytest retains the product guard and reports the actual dependency; add
`--core-checkout=/path/to/core` to require a particular core. Use the equals form
so pytest can locate its configuration before parsing the extra option.
For deliberate wheel or other installed-package testing, use `--installed-package`;
the output labels that mode. This exception is not checkout verification.

CI uses the existing setup/browser/check commands on Python 3.12 and an explicit
supported core revision. Browser libraries are installed on the runner separately.
Changing that revision requires the same PostgreSQL and browser verification.
Local success does not establish published CI execution or a hosted deployment.

## Keep evidence before handing off

Store sanitized logs in the project's local verification-artifact directory,
outside disposable goal scratch. The retrospective task uses
`../.verification/ledgerlens/2026-10-06/` relative to its task checkout, with a
receipt under `../.scratch/retrospectives/evidence/ledgerlens-verification.md`.
Other tasks may use their existing review-artifact location. Logs on one machine
are not available in a fresh clone: transfer sanitized logs when changing machines
or mark unavailable evidence unverified. Never include credentials, private rows,
`.env` files, or signing keys.

Record:

- Project, task owner, worktree, branch, exact base/head revisions, spec pointers,
  and dirty files excluded from the reviewed diff.
- Interpreter, resolved source paths, dependency revisions, exact commands,
  exit codes, date, test counts, skips, and retained log locations.
- Review duration, reproduced actionable defects, repeated verification or
  handoff work, and one next action. Add token/cost figures only if reliable.
- Separate local checks, remote CI, and hosted verification. A skip, missing log,
  timeout, or static review is not runtime proof.

The builder commits locally and ends with `HANDOFF: INTENT`: what changed, why,
base/head revisions, scope, and verification. A fresh driver owns review/gate
work. Review the same fixed diff and spec, declare coverage, and resolve defects
with counterexamples. Keep the standards/spec axes of the existing review skill.
Before parking a driver, stop or checkpoint the gate and preserve resumable
state. Never end the task while it owns a live gate. Remote publication follows
the user's command restrictions; a pending terminal action is a separate state.

Keep accepted terms in `CONTEXT.md` and decisions in `docs/adr/`. A temporary
handoff links to these and the receipt rather than copying their contents.
Retain logs until the task is reconciled and its receipt archived, then review
them for cleanup. Missing evidence is always labeled unverified.
