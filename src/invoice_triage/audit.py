"""Read-only presentation of core AuditEvent records for a checked run."""

from airflow_dq_agent.contracts.models import AuditEvent
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


def read_audit_events(engine: Engine, run_ids: list[str]) -> list[AuditEvent]:
    # The core lookup API only accepts one event ID. Run history needs this
    # read-only query; validation uses the core's public model, not its parser.
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


def audit_labels(events: list[AuditEvent], report_available: bool) -> dict[str, str]:
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
