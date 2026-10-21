"""Staging concurrency guards: singleton staging, verdict binding, resolve gate."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy.event as sa_event
from airflow_dq_agent.warehouse.db import make_engine
from fastapi.testclient import TestClient
from sqlalchemy import text

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _staging_rows(pg_dsns: dict[str, str]) -> list[tuple[str, int]]:
    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT invoice_ref, version FROM wb.staging_invoice")
        ).all()
    return [(str(r[0]), int(r[1])) for r in rows]


def test_concurrent_ingests_keep_singleton_staging(
    pg_dsns: dict[str, str],
) -> None:
    """Two overlapping uploads must never leave two staging rows.

    A barrier on the staging DELETE forces DELETE/DELETE overlap: without
    serialization both uploads commit (2 rows); with the ingest lock the
    second upload waits and replaces the first (1 row).
    """
    from invoice_triage.services import ingest_submission

    engine = make_engine(pg_dsns["intake"])
    barrier = threading.Barrier(2)
    payloads = {
        "missing_amount.csv": (FIXTURES / "missing_amount.csv").read_bytes(),
        "valid_control.csv": (FIXTURES / "valid_control.csv").read_bytes(),
    }

    def _after_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "DELETE FROM wb.staging_invoice" in statement:
            try:
                barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass

    sa_event.listen(engine, "after_cursor_execute", _after_execute)
    errors: list[BaseException] = []

    def _ingest(name: str) -> None:
        try:
            ingest_submission(engine, filename=name, data=payloads[name])
        except BaseException as exc:  # propagate to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=_ingest, args=(name,)) for name in payloads]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
    finally:
        sa_event.remove(engine, "after_cursor_execute", _after_execute)
    assert not errors
    assert len(_staging_rows(pg_dsns)) == 1


def test_run_after_staging_replacement_is_rejected_without_verdict(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report must never bind to a submission it did not check.

    When another upload replaces staging between staged capture and the
    core read, the run is rejected (HTTP 409) and no verdict is stored.
    """
    from airflow_dq_agent.quality.suite import run_quality_suite as real_suite

    import invoice_triage.services as services

    def _suite_with_interleaved_upload(dsn: str) -> Any:
        # An out-of-band database writer bypasses the intake lock. The binding
        # recheck must still fail closed rather than attach mismatched evidence.
        with make_engine(pg_dsns["admin"]).begin() as conn:
            conn.execute(text("UPDATE wb.staging_invoice SET version = version + 1"))
        return real_suite(dsn)

    monkeypatch.setattr(services, "run_quality_suite", _suite_with_interleaved_upload)
    first = client.post(
        "/ingest",
        files={
            "file": (
                "missing_amount.csv",
                (FIXTURES / "missing_amount.csv").read_bytes(),
                "text/csv",
            )
        },
        headers=api_headers,
    )
    assert first.status_code == 200
    run = client.post("/runs", headers=api_headers)
    assert run.status_code == 409
    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM wb.case_verdicts")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM wb.cases")).scalar() == 0


def test_resolve_after_concurrent_upload_refuses_stale_pass(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    """Resolve must recheck staging atomically at commit time.

    When a new upload commits after the gate reads staging but before
    its final UPDATE, the case must stay open instead of resolving on a
    stale pass.
    """
    from invoice_triage.services import resolve_case

    for name in ("missing_amount.csv", "corrected_amount.csv"):
        ingest = client.post(
            "/ingest",
            files={"file": (name, (FIXTURES / name).read_bytes(), "text/csv")},
            headers=api_headers,
        )
        assert ingest.status_code == 200
        assert client.post("/runs", headers=api_headers).status_code == 200
    overview = client.get("/", headers=api_headers).json()
    assert len(overview["cases"]) == 1
    case_id = str(overview["cases"][0]["id"])

    engine = make_engine(pg_dsns["intake"])
    updater = make_engine(pg_dsns["admin"])
    displaced = {"done": False}

    def _after_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if (
            not displaced["done"]
            and "FROM wb.staging_invoice" in statement
            and "WHERE invoice_ref" in statement
        ):
            displaced["done"] = True
            # Privileged out-of-band mutation bypasses normal intake locking.
            with updater.begin() as update_conn:
                update_conn.execute(text("UPDATE wb.staging_invoice SET version = 3"))

    sa_event.listen(engine, "after_cursor_execute", _after_execute)
    try:
        outcome = resolve_case(
            intake=engine, audit=make_engine(pg_dsns["audit"]), case_id=case_id
        )
    finally:
        sa_event.remove(engine, "after_cursor_execute", _after_execute)
    assert displaced["done"]
    assert (outcome["resolved"], outcome["reason"]) == (False, "stale-version")
    assert (
        client.get(f"/cases/{case_id}", headers=api_headers).json()["status"] == "open"
    )


def test_overlapping_first_checks_preserve_both_run_verdicts(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    """Force the first-case race at SQL boundary, observe the public case history."""
    from concurrent.futures import ThreadPoolExecutor

    from invoice_triage.services import get_case_detail, run_current_checks

    client.post(
        "/ingest",
        files={"file": ("missing.csv", (FIXTURES / "missing_amount.csv").read_bytes())},
        headers=api_headers,
    ).raise_for_status()
    engine = make_engine(pg_dsns["intake"])
    barrier = threading.Barrier(2)

    def overlap(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "SELECT id FROM wb.cases" in statement:
            try:
                barrier.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass

    sa_event.listen(engine, "after_cursor_execute", overlap)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                pool.submit(run_current_checks, intake=engine, read_dsn=pg_dsns["read"])
                for _ in range(2)
            ]
            runs = [job.result(timeout=20) for job in jobs]
    finally:
        sa_event.remove(engine, "after_cursor_execute", overlap)
    assert runs[0]["case_id"] == runs[1]["case_id"]
    case = get_case_detail(
        intake=engine,
        audit=make_engine(pg_dsns["audit"]),
        case_id=str(runs[0]["case_id"]),
    )
    history = case["history"]
    assert isinstance(history, dict)
    assert {v["run_id"] for v in history["verdicts"]} == {run["run_id"] for run in runs}


def test_check_keeps_staging_stable_through_core_read(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    """A SQL callback creates an upload while the real core read is pending.

    Intake must wait until the checked snapshot and its verdict commit; an
    identity recheck alone cannot detect an A-to-B-to-A staging replacement.
    """
    from concurrent.futures import ThreadPoolExecutor

    from invoice_triage.services import ingest_submission, run_current_checks

    client.post(
        "/ingest",
        files={"file": ("missing.csv", (FIXTURES / "missing_amount.csv").read_bytes())},
        headers=api_headers,
    ).raise_for_status()
    engine = make_engine(pg_dsns["intake"])
    uploader = make_engine(pg_dsns["intake"])
    attempted = threading.Event()
    finished = threading.Event()
    started = False

    def upload() -> None:
        try:
            ingest_submission(
                uploader,
                filename="valid.csv",
                data=(FIXTURES / "valid_control.csv").read_bytes(),
            )
        finally:
            finished.set()

    def before_upload(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def after_capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            nonlocal started
            if "FROM wb.staging_invoice LIMIT 1" in statement and not started:
                started = True
                pool.submit(upload)
                assert attempted.wait(timeout=5)
                assert not finished.wait(timeout=0.3), "upload replaced checked staging"

        sa_event.listen(engine, "after_cursor_execute", after_capture)
        sa_event.listen(uploader, "before_cursor_execute", before_upload)
        try:
            result = run_current_checks(intake=engine, read_dsn=pg_dsns["read"])
        finally:
            sa_event.remove(engine, "after_cursor_execute", after_capture)
            sa_event.remove(uploader, "before_cursor_execute", before_upload)
    assert started and finished.is_set()
    assert (result["invoice_ref"], result["status"]) == ("INV-001", "fail")
    case = client.get(f"/cases/{result['case_id']}", headers=api_headers).json()
    assert case["row"]["amount_minor"] is None
    assert case["verdict"] == "fail"


def test_resolution_keeps_staging_stable_until_commit(
    client: TestClient,
    api_headers: dict[str, str],
    pg_dsns: dict[str, str],
) -> None:
    """Interleave intake after gate reads; HTTP alone cannot force this overlap."""
    from concurrent.futures import ThreadPoolExecutor

    from invoice_triage.services import ingest_submission, resolve_case

    for name in ("missing_amount.csv", "corrected_amount.csv"):
        client.post(
            "/ingest",
            files={"file": (name, (FIXTURES / name).read_bytes())},
            headers=api_headers,
        ).raise_for_status()
        run = client.post("/runs", headers=api_headers).json()
    case_id = run["case_id"]
    engine = make_engine(pg_dsns["intake"])
    uploader = make_engine(pg_dsns["intake"])
    attempted = threading.Event()
    finished = threading.Event()

    def upload() -> None:
        try:
            ingest_submission(
                uploader,
                filename="valid.csv",
                data=(FIXTURES / "valid_control.csv").read_bytes(),
            )
        finally:
            finished.set()

    def before_upload(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "pg_advisory_xact_lock" in statement:
            attempted.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def before_update(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            if "UPDATE wb.cases SET status" in statement:
                pool.submit(upload)
                assert attempted.wait(timeout=5)
                assert not finished.wait(timeout=0.3), "upload overtook resolution"

        sa_event.listen(engine, "before_cursor_execute", before_update)
        sa_event.listen(uploader, "before_cursor_execute", before_upload)
        try:
            outcome = resolve_case(
                intake=engine, audit=make_engine(pg_dsns["audit"]), case_id=case_id
            )
        finally:
            sa_event.remove(engine, "before_cursor_execute", before_update)
            sa_event.remove(uploader, "before_cursor_execute", before_upload)
    assert finished.is_set()
    assert outcome["resolved"] is True
    assert (
        client.get(f"/cases/{case_id}", headers=api_headers).json()["status"]
        == "resolved"
    )
