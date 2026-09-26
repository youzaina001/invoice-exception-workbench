# LedgerLens (invoice-exception-workbench)

Operator workbench for investigating incomplete supplier invoices against the airflow-dq-agent governance core.

Display name: LedgerLens. Repository name: invoice-exception-workbench.

## Status

Base commit only. Milestone 1 (one complete operator journey) is built in the feature worktree on branch `feature/m1-operator-journey`; see that branch for the app, fixtures, tests, and replay steps.

## Tested core revision

- Core: `/home/youzaina001/agy-projects/projects/airflow-dq-agent/` at commit `0371013` (merge PR #94).
- Repro: `git -C /home/youzaina001/agy-projects/projects/airflow-dq-agent log --oneline -1` shows `0371013`; M1 pins this revision and re-verifies registry, suite, traces, and adoption helpers before wiring.
- Core settings for M1: `LLM_MODE=stub`, `APPLY_MODE=off`, `TRACE_POSTGRES=true`.

## Resolution rule (M1)

A case closes only through an explicit operator Resolve action gated on a fresh pass of the required checks for the current submission version. Stale, failed, errored, incomplete, or mismatched reports never resolve. Quarantine success, rejection, and timeout are distinct from resolution. A resolved case means configured checks passed for a specific version, not payment approval.

## Limits

Localhost-only synthetic demo; no login in M1. Document AI/OCR, Airflow HITL trigger, reconciliation, and downstream publication are later milestones and are not scaffolded here.
