# PR #1 review follow-up

Scope: `master...HEAD` plus the uncommitted fixes in
`feature/m1-operator-journey`. Requirements were checked against the Muse M1
prompt and the 2026-09-26 M1 decisions and core integration notes.

## Fixed

- Removed unused runtime admin DSN and host/port settings. `make run` supplies
  the localhost bind; the browser acceptance test starts that exact command.
- Added setup, database, run, test, lint, typecheck, and combined check targets.
  README now separates the two foreground demo terminals and lists all three
  runtime role DSNs with non-secret placeholders.
- Errored checks open a case and refuse resolution with `errored-report`;
  missing/unreadable reports remain `incomplete-report`. Operator guidance
  distinguishes the two recovery paths.
- Unknown audit labels use the specified casing. Stored approval reviews,
  human decisions, and successful quarantine-copy evidence are read for the
  relevant runs, displayed without resolving a case, and retained in history
  after a corrected version is checked. No live Airflow integration is claimed.
- Audit reads use one app-owned engine, disposed at shutdown. The core's public
  single-ID repository cannot query a run's history or accept an existing engine;
  this small read-only adapter queries `dq.traces` and validates its records with
  the public `AuditEvent` model. No private core deserializer or upstream change.
- Staging reads reuse the connection holding the lock; the identity query and
  lock acquisition are shared. Resolve races reuse the ordinary refusal gate.
- Preserved the incoming upload-size, precise decimal, role-DDL quoting,
  PostgreSQL password-authentication, and audit-value-leak regression fixes.
  Added a bound check before arithmetic so extreme exponents also return 400.

## Judgement findings retained with reasons

| Finding | Disposition |
| --- | --- |
| Singleton staging displaces other invoices | The integration decisions explicitly choose current-submission-only staging; core checks read the whole table. Keep the guard, document restaging, and retain the cross-invoice tests. Per-invoice concurrent checking requires a separate integration change. |
| Duplicate-restage UX | Required recovery for the singleton limit, with immutable history and no duplicate cases. Older duplicates cannot regress current staging. |
| Locks and concurrency tests | Synchronous FastAPI endpoints can overlap in threads. These protect the required snapshot and stale-result guarantees; removing them would regress correctness. |
| Extra validation fixtures | Exercise the required untrusted CSV boundary and integer money range, including the discovered rounding and overflow bugs. Three fixtures remain the operator demo; the rest are rejection tests. |
| JSON responses | Content negotiation uses the same actions and resolution gate as HTML. Retained for existing integration tests and local inspection; there is no second frontend or independent workflow. |
| Large services module / loose staging fields | Audit reading is now separate; repeated lock, staging, and refusal logic is consolidated. Additional layers or domain types would add indirection without another consumer. |
| Repeated small test helpers / amount display | Small local helpers and explicit `None` formatting remain; a generic helper or filter offers little value and must not treat zero as missing. |

## Limits and outstanding access restrictions

The pinned core does not persist a shadow/HITL mode discriminator in approval
review events. The UI says mode Unknown rather than guessing. Audit display
tests append synthetic typed core records; they verify reading and presentation,
not execution of Airflow approvals or quarantine.

The sandbox denies `.env.example` access, including its already-modified local
content. It was neither inspected nor changed by this follow-up. The complete
non-secret environment example is in README; reconcile the root example in a
terminal with access before publishing.

Git metadata resides outside this writable workspace, so committing may require
the user's terminal. Guarded YOLO forbids push and PR writes here. PR #1 was
already open and non-draft; local fixes must be committed and pushed before the
remote PR includes them.
