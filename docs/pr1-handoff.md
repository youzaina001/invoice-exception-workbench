# PR #1 readiness handoff

Repository: invoice-exception-workbench. Branch: feature/m1-operator-journey.
Worktree: /home/youzaina001/agy-projects/projects/invoice-exception-workbench-m1.
PR: https://github.com/youzaina001/invoice-exception-workbench/pull/1.
Base: master. Published head before this follow-up: e5ba5a6.

## HANDOFF: INTENT

Make the local M1 operator journey reviewable without adding an executor or
changing its singleton staging contract. Preserve the incoming fixes; remove
unused runtime credential/bind settings; distinguish errored checks from missing
audit; display stored typed core audit evidence and history without inferring
resolution; reuse app-owned audit connections; consolidate staging/resolve guard
logic; reject extreme amounts before decimal arithmetic. Add a Makefile and
reproducible setup, run, environment, and review documentation. See
`review-follow-up.md` for each finding and retained M1 judgement limit.

User explicitly authorized continuing in this existing worktree after fresh
worktree creation failed. No environment/credential files were read or changed.
The pre-existing `.env.example` change remains unverified due to sandbox denial.

## Verification (2026-09-27)

- `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt make check`: **66 passed**;
  Ruff lint/format clean; strict mypy clean (21 Python source files).
- Includes real PostgreSQL, installed core 0371013 (0.2.0), and Chromium driving
  the server launched by `make run`. No live Airflow execution was added.
- Red checks observed for errored-report reasons, Unknown casing, associated
  audit labels, audit history after correction, missing `make run`, and extreme
  decimal overflow; the implementations then passed the targeted checks.
- `make -n setup browser-install db`: recipes expand correctly. Fresh dependency
  installation was not repeated; acceptance ran in the existing virtualenv.
- `/tmp/kilo/verify_pg.py`: SCRAM rejects wrong passwords; password persists on
  restart; missing password is refused; CREATE/ALTER role setup works and old
  credentials fail after rotation. This is a scratch verification, not a shipped
  Airflow or migration test.
- `git diff --check -- README.md src tests fixtures Makefile docs`: clean.

## Review and publication

Independent local code review: complete. See [PR #1 review](pr1-review.md).
The one low-severity README wording mismatch was fixed; no spec findings.

Changes are **uncommitted and unpublished**. Staging failed with:
`Unable to create .../.git/worktrees/invoice-exception-workbench-m1/index.lock:
Read-only file system`.
The failed staging command was:

```bash
git add README.md Makefile docs/review-follow-up.md src/ledgerlens tests fixtures/long_subcent_amount.csv
```

For the complete local change set, run this staging command in your own
terminal after reviewing the changes:

```bash
git add README.md Makefile docs src/ledgerlens tests fixtures/long_subcent_amount.csv
```

Also inspect and reconcile `.env.example` against README's three-role example
in a terminal with access before staging it. Commit the reviewed files and push the
feature branch from your terminal; Guarded YOLO forbids push/PR writes here.
PR #1 was already open and non-draft, but its remote diff does not contain these
local changes until that push. Do not mark the goal complete before verifying
the published head and review result.
