"""Concurrent HTTP journeys coordinated at the real PostgreSQL boundary."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import Any, cast

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from conftest import signed_in_reviewer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from invoice_triage.app import create_app
from invoice_triage.settings import Settings


@contextmanager
def hosted(pg_dsns: dict[str, str]) -> Iterator[TestClient]:
    with TestClient(
        create_app(
            Settings(
                hosted=True,
                intake_dsn=pg_dsns["intake"],
                read_dsn=pg_dsns["read"],
                audit_dsn=pg_dsns["audit"],
            ),
            authenticate=signed_in_reviewer,
        )
    ) as client:
        yield client


def wait_for_blocked(owner: Any, count: int) -> None:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with owner.connect() as conn:
            blocked = conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE usename = 'wb_intake' AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        if blocked >= count:
            return
        sleep(0.01)
    raise AssertionError(f"Expected {count} blocked requests")


def test_simultaneous_first_visits_share_one_initial_attempt(
    pg_dsns: dict[str, str],
) -> None:
    owner = make_engine(pg_dsns["admin"])
    headers = {"Accept": "application/json", "X-Reviewer": "first-concurrent"}
    with hosted(pg_dsns) as client, ThreadPoolExecutor(2) as pool:
        with owner.begin() as blocker:
            blocker.execute(text("LOCK TABLE wb.demo_workspaces IN SHARE MODE"))
            pending = [pool.submit(client.get, "/", headers=headers) for _ in range(2)]
            wait_for_blocked(owner, 2)
        responses = [job.result(timeout=15) for job in pending]
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    with owner.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM wb.demo_workspaces")).scalar_one()
            == 1
        )
        assert (
            conn.execute(text("SELECT count(*) FROM wb.demo_attempts")).scalar_one()
            == 1
        )
    owner.dispose()


def test_simultaneous_start_fresh_keeps_five_with_one_current(
    pg_dsns: dict[str, str],
) -> None:
    owner = make_engine(pg_dsns["admin"])
    headers = {"Accept": "application/json", "X-Reviewer": "fresh-concurrent"}
    with hosted(pg_dsns) as client, ThreadPoolExecutor(2) as pool:
        client.get("/", headers=headers)
        for _ in range(3):
            assert (
                client.post("/demo-attempts/start-fresh", headers=headers).status_code
                == 200
            )
        with owner.begin() as blocker:
            blocker.execute(text("LOCK TABLE wb.demo_attempts IN SHARE MODE"))
            pending = [
                pool.submit(client.post, "/demo-attempts/start-fresh", headers=headers)
                for _ in range(2)
            ]
            wait_for_blocked(owner, 2)
        responses = [job.result(timeout=15) for job in pending]
        assert [r.status_code for r in responses] == [200, 200]
        current = client.get("/", headers=headers).json()["attempt_id"]
        with owner.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM wb.demo_attempts")).scalar_one()
                == 5
            )
        transitions = {
            r.json()["previous_attempt_id"]: r.json()["attempt_id"] for r in responses
        }
        assert current in transitions.values()
        assert len(set(transitions) & set(transitions.values())) == 1
    owner.dispose()


@pytest.mark.parametrize("action", ["ingest", "runs", "resolve"])
def test_inflight_action_cannot_write_after_start_fresh(
    pg_dsns: dict[str, str], action: str
) -> None:
    headers = {"Accept": "application/json", "X-Reviewer": "paused-action"}
    csv_root = Path(__file__).resolve().parent.parent / "fixtures"
    with hosted(pg_dsns) as client, ThreadPoolExecutor(1) as pool:

        def upload(name: str) -> Any:
            return client.post(
                "/ingest",
                headers=headers,
                files={"file": (name, (csv_root / name).read_bytes(), "text/csv")},
            )

        assert upload("missing_amount.csv").status_code == 200
        case_id = client.post("/runs", headers=headers).json()["case_id"]
        if action == "resolve":
            assert upload("corrected_amount.csv").status_code == 200
            assert client.post("/runs", headers=headers).status_code == 200
        entered, release = Event(), Event()

        def pause_at_database(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            # Pause the first mutation connection after HTTP selected its attempt.
            if "set_config('app.attempt'" in statement and not entered.is_set():
                entered.set()
                assert release.wait(15)

        engine = cast(FastAPI, client.app).state.intake_engine
        event.listen(engine, "after_cursor_execute", pause_at_database)
        try:
            if action == "ingest":
                future = pool.submit(upload, "corrected_amount.csv")
            else:
                path = "/runs" if action == "runs" else f"/cases/{case_id}/resolve"
                future = pool.submit(client.post, path, headers=headers)
            assert entered.wait(10)
            started = client.post("/demo-attempts/start-fresh", headers=headers)
            assert started.status_code == 200
            previous = started.json()["previous_attempt_id"]
            before = client.get("/demo-attempts/" + previous, headers=headers).json()
            release.set()
            response = future.result(timeout=15)
            assert response.status_code == 409
            assert response.json()["reason"] == "attempt-read-only"
            assert (
                client.get("/demo-attempts/" + previous, headers=headers).json()
                == before
            )
            assert client.get("/", headers=headers).json()["submissions"] == []
        finally:
            release.set()
            event.remove(engine, "after_cursor_execute", pause_at_database)
