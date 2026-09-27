"""Pure case-resolution rule. No I/O; units pin every refusal reason."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolutionRequest:
    case_status: str
    current_version: int
    current_snapshot: str
    verdict_version: int
    verdict_snapshot: str
    verdict_status: str
    verdict_check_id: str
    required_check_id: str
    report_complete: bool


@dataclass(frozen=True)
class ResolutionDecision:
    allowed: bool
    reason: str


def decide_resolution(request: ResolutionRequest) -> ResolutionDecision:
    if request.case_status != "open":
        return ResolutionDecision(False, "case-not-open")
    if not request.report_complete:
        return ResolutionDecision(False, "incomplete-report")
    if request.verdict_check_id != request.required_check_id:
        return ResolutionDecision(False, "check-mismatch")
    if request.verdict_version != request.current_version:
        return ResolutionDecision(False, "stale-version")
    if request.verdict_snapshot != request.current_snapshot:
        return ResolutionDecision(False, "snapshot-mismatch")
    if request.verdict_status != "pass":
        return ResolutionDecision(False, "verdict-not-pass")
    return ResolutionDecision(True, "ok")


REFUSAL_GUIDANCE = {
    "case-not-open": "This case is already resolved; no action is needed.",
    "incomplete-report": (
        "The check report is incomplete or its audit trace is unavailable; "
        "run checks again, then resolve."
    ),
    "check-mismatch": (
        "The latest verdict is for a different check; run the required checks."
    ),
    "stale-version": (
        "A newer submission exists; run checks on the current version first."
    ),
    "snapshot-mismatch": (
        "The staged row changed since the check run; run checks again first."
    ),
    "verdict-not-pass": (
        "The latest verdict did not pass; ingest a corrected CSV first."
    ),
    "no-verdict": "No check run is recorded for this case; run checks first.",
    "current-missing": (
        "This invoice is no longer staged; re-upload its latest CSV,"
        " run checks, then resolve."
    ),
}


def refusal_guidance(reason: str) -> str:
    """Operator-facing recovery hint for a resolve refusal reason."""
    return REFUSAL_GUIDANCE.get(
        reason, "Resolve was refused; run checks and try again."
    )
