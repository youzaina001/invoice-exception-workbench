"""Sole adapter over the installed airflow_dq_agent governance core."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from airflow_dq_agent.adoption import (
    apply_governance_schema,
    provision_restricted_logins,
)
from airflow_dq_agent.contracts.models import AuditEvent, Dimension, QualitySuiteReport
from airflow_dq_agent.contracts.tables import (
    TABLE_CONTRACTS,
    ColumnContract,
    TableContract,
    register_contract,
)
from airflow_dq_agent.quality.registry import (
    CHECK_SPECS,
    CheckPolicy,
    CheckSpec,
    register_check,
)
from airflow_dq_agent.quality.suite import run_quality_suite
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

STAGING_TABLE = "staging_invoice"
STAGING_SCHEMA = "wb"
AMOUNT_CHECK_ID = "staging_invoice.amount_minor.completeness"

_registration_lock = Lock()
_ENV_KEYS = (
    "TRACE_POSTGRES",
    "AUDIT_DSN",
    "LLM_MODE",
    "APPLY_MODE",
    "TRACES_DIR",
)


class CheckMissingError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckVerdict:
    run_id: str
    report_id: str
    check_id: str
    status: str
    audit_event_id: str | None
    n_failed: int
    n_total: int
    message: str
    sample_failures: list[dict[str, Any]]


@dataclass(frozen=True)
class CaseAuditView:
    report_available: bool
    labels: dict[str, str]
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class GovernanceCredentials:
    owner_dsn: str
    read_dsn: str
    audit_dsn: str
    apply_dsn: str


def _ensure_registered() -> None:
    """Register the staging contract and amount check once per process."""
    with _registration_lock:
        if STAGING_TABLE not in TABLE_CONTRACTS:
            register_contract(
                TableContract(
                    table=STAGING_TABLE,
                    schema_name=STAGING_SCHEMA,
                    grain="single row: the current submission",
                    description="Workbench-owned staging projection.",
                    primary_key=["invoice_ref"],
                    columns=[
                        ColumnContract(name="invoice_ref", dtype="utf8", unique=True),
                        ColumnContract(name="invoice_id", dtype="utf8"),
                        ColumnContract(name="supplier_id", dtype="utf8"),
                        ColumnContract(name="invoice_date", dtype="date"),
                        ColumnContract(name="currency", dtype="utf8"),
                        ColumnContract(
                            name="amount_minor", dtype="int64", nullable=True
                        ),
                    ],
                )
            )
        if AMOUNT_CHECK_ID not in CHECK_SPECS:
            register_check(
                CheckSpec(
                    check_id=AMOUNT_CHECK_ID,
                    table=STAGING_TABLE,
                    column="amount_minor",
                    dimension=Dimension.COMPLETENESS,
                    description="amount must be present",
                    policies=[CheckPolicy(action_id="quarantine_nulls")],
                )
            )


def _install_engine_scope(scope_installer: Callable[[Engine], None]) -> None:
    """Stamp Reviewer settings on engines the quality core opens itself."""
    import airflow_dq_agent.quality.suite as suite
    import airflow_dq_agent.traces.writer as writer
    import airflow_dq_agent.warehouse.db as warehouse_db

    if getattr(warehouse_db.make_engine, "_ledgerlens_scoped", False):
        return
    original = warehouse_db.make_engine

    def make_engine(dsn: str | None = None) -> Engine:
        engine = original(dsn)
        scope_installer(engine)
        return engine

    setattr(make_engine, "_ledgerlens_scoped", True)
    setattr(warehouse_db, "make_engine", make_engine)
    setattr(suite, "make_engine", make_engine)
    setattr(writer, "make_engine", make_engine)


def _read_audit_events(engine: Engine, run_ids: list[str]) -> list[AuditEvent]:
    # The core lookup API only accepts one event ID. Run history needs this
    # read-only query; validation uses the core's public model, not its parser.
    if not run_ids:
        return []
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT trace_id, kind, body FROM dq.traces"
                    " WHERE body->>'quality_run_id' = ANY(:runs)"
                ),
                {"runs": run_ids},
            ).all()
    except SQLAlchemyError:
        return []
    events = []
    for trace_id, kind, body in rows:
        try:
            event = AuditEvent.model_validate(body)
        except ValueError:
            continue
        if event.event_id == trace_id and event.kind == kind:
            events.append(event)
    return sorted(events, key=lambda event: event.occurred_at)


def _audit_labels(events: list[AuditEvent], report_available: bool) -> dict[str, str]:
    labels = dict.fromkeys(
        (
            "shadow_review",
            "approval",
            "human_decision",
            "quarantine",
            "rejection",
            "timeout",
        ),
        "Unknown",
    )
    labels["quality_report"] = "recorded" if report_available else "Unknown"
    decisions = {
        "human_approved": ("approval", "Approved"),
        "human_rejected": ("rejection", "Rejected"),
        "human_timed_out": ("timeout", "Timed out"),
    }
    for event in events:
        if event.kind == "approval_review":
            # This core revision does not persist shadow/HITL execution mode.
            labels["shadow_review"] = "Review recorded (mode Unknown)"
        elif event.kind in decisions:
            label, value = decisions[event.kind]
            labels[label] = value
            labels["human_decision"] = value
        elif event.kind == "apply_succeeded" and any(
            step.action_id == "quarantine_nulls"
            and step.table == "staging_invoice"
            and step.rowcount is not None
            and step.rowcount > 0
            for step in event.apply_steps
        ):
            labels["quarantine"] = "Copy recorded"
    return labels


def configure_governance(
    *,
    audit_dsn: str | None = None,
    traces_dir: str | Path | None = None,
    scope_installer: Callable[[Engine], None] | None = None,
) -> dict[str, str | None]:
    """Ensure registration, hook core engine scoping, and bind audit environment."""
    _ensure_registered()
    if scope_installer is not None:
        _install_engine_scope(scope_installer)
    previous = {key: os.environ.get(key) for key in _ENV_KEYS}
    if audit_dsn is not None or traces_dir is not None:
        os.environ["TRACE_POSTGRES"] = "true"
        os.environ["LLM_MODE"] = "stub"
        os.environ["APPLY_MODE"] = "off"
        if audit_dsn is not None:
            os.environ["AUDIT_DSN"] = audit_dsn
        if traces_dir is not None:
            os.environ["TRACES_DIR"] = str(traces_dir)
    return previous


def execute_amount_check(
    read_dsn: str,
    *,
    _suite_runner: Callable[[str], QualitySuiteReport] | None = None,
) -> CheckVerdict:
    """Register contracts and run the staging amount completeness check."""
    _ensure_registered()
    report = (_suite_runner or run_quality_suite)(read_dsn)
    result = report.get(AMOUNT_CHECK_ID)
    if result is None:
        raise CheckMissingError(f"required check {AMOUNT_CHECK_ID} did not run")
    return CheckVerdict(
        run_id=report.run_id,
        report_id=report.report_id,
        check_id=AMOUNT_CHECK_ID,
        status=result.status.value,
        audit_event_id=report.audit_event_id,
        n_failed=result.n_failed,
        n_total=result.n_total,
        message=result.message,
        sample_failures=result.sample_failures,
    )


def read_audit_lineage(
    audit: Engine,
    *,
    verdicts: Sequence[Mapping[Any, Any]] = (),
    latest_verdict: Mapping[Any, Any] | None = None,
    attempt_id: str | None = None,
) -> CaseAuditView:
    """Read fail-open audit lineage for a case's runs or a Demo Attempt."""
    if attempt_id is not None:
        try:
            with audit.connect() as conn:
                rows = (
                    conn.execute(
                        text(
                            "SELECT trace_id, kind, body->>'quality_run_id' AS run_id"
                            " FROM dq.traces WHERE attempt_id = :attempt"
                            " ORDER BY recorded_at, seq"
                        ),
                        {"attempt": attempt_id},
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError:
            rows = []
        return CaseAuditView(
            report_available=False,
            labels=_audit_labels([], False),
            events=[
                {
                    "event_id": row["trace_id"],
                    "run_id": row["run_id"],
                    "kind": row["kind"],
                }
                for row in rows
            ],
        )

    run_ids = list(
        dict.fromkeys(
            [str(v["run_id"]) for v in verdicts if v.get("run_id") is not None]
            + (
                [str(latest_verdict["run_id"])]
                if latest_verdict is not None
                and latest_verdict.get("run_id") is not None
                else []
            )
        )
    )
    audit_events = _read_audit_events(audit, run_ids)
    report_available = (
        latest_verdict is not None
        and latest_verdict.get("audit_event_id") is not None
        and any(
            event.event_id == str(latest_verdict["audit_event_id"])
            and event.kind == "quality_report"
            and event.quality_run_id == str(latest_verdict["run_id"])
            and event.report_id == str(latest_verdict["report_id"])
            for event in audit_events
        )
    )
    latest_run_id = (
        str(latest_verdict["run_id"]) if latest_verdict is not None else None
    )
    latest_events = [
        event
        for event in audit_events
        if latest_run_id is not None and event.quality_run_id == latest_run_id
    ]
    labels = _audit_labels(latest_events, report_available)
    events = [
        {
            "event_id": event.event_id,
            "run_id": event.quality_run_id,
            "kind": event.kind,
            "label": event.kind.replace("_", " ").capitalize(),
            "occurred_at": event.occurred_at.isoformat(),
        }
        for event in audit_events
    ]
    return CaseAuditView(
        report_available=report_available,
        labels=labels,
        events=events,
    )


def provision_governance_schema(admin_dsn: str) -> GovernanceCredentials:
    """Install the governance schema and provision least-privilege capability logins."""
    apply_governance_schema(admin_dsn)
    creds = provision_restricted_logins(admin_dsn)
    return GovernanceCredentials(
        owner_dsn=creds.owner_dsn,
        read_dsn=creds.read_dsn,
        audit_dsn=creds.audit_dsn,
        apply_dsn=creds.apply_dsn,
    )


def core_identity(pinned_revision: str) -> dict[str, str]:
    """Verify the imported airflow_dq_agent checkout matches the pinned revision."""
    import airflow_dq_agent

    origin = Path(airflow_dq_agent.__file__).resolve()
    result = subprocess.run(
        ["git", "-C", str(origin.parent), "rev-parse", "--show-toplevel", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    root, head = result.stdout.strip().splitlines()
    if (
        Path(root) / "src/airflow_dq_agent/__init__.py" != origin
        or head != pinned_revision
    ):
        raise ValueError("workflow replay requires the pinned core checkout")
    dirty = subprocess.run(
        ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
        check=True,
        capture_output=True,
        text=True,
    )
    if dirty.stdout:
        raise ValueError("pinned core has tracked changes")
    return {"source": str(origin), "revision": head, "call": "in-process run_suite"}
