"""Resolution rule units: every refusal reason pinned, plus the allow path."""

from __future__ import annotations

from invoice_triage.core_integration import AMOUNT_CHECK_ID
from invoice_triage.resolution import ResolutionRequest, decide_resolution


def fresh_pass() -> ResolutionRequest:
    return ResolutionRequest(
        case_status="open",
        current_version=2,
        current_snapshot="snap2",
        verdict_version=2,
        verdict_snapshot="snap2",
        verdict_status="pass",
        verdict_check_id=AMOUNT_CHECK_ID,
        required_check_id=AMOUNT_CHECK_ID,
        report_complete=True,
    )


def test_fresh_pass_on_current_version_allows_resolution() -> None:
    decision = decide_resolution(fresh_pass())
    assert (decision.allowed, decision.reason) == (True, "ok")


def test_closed_case_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "case_status", "resolved")
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "case-not-open")


def test_incomplete_report_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "report_complete", False)
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "incomplete-report")


def test_wrong_check_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "verdict_check_id", "other.check")
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "check-mismatch")


def test_stale_version_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "verdict_version", 1)
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "stale-version")


def test_changed_snapshot_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "verdict_snapshot", "snap1")
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "snapshot-mismatch")


def test_failed_verdict_cannot_resolve() -> None:
    req = fresh_pass()
    object.__setattr__(req, "verdict_status", "fail")
    decision = decide_resolution(req)
    assert (decision.allowed, decision.reason) == (False, "verdict-not-pass")


def test_errored_report_is_distinct_from_incomplete_report() -> None:
    from dataclasses import replace

    decision = decide_resolution(replace(fresh_pass(), verdict_status="error"))
    assert (decision.allowed, decision.reason) == (False, "errored-report")
