"""M1 fixtures: real embedded Postgres + installed core, reset between tests."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--installed-package",
        action="store_true",
        help="Test an installed LedgerLens package instead of this checkout",
    )
    parser.addoption(
        "--core-checkout",
        help="Require airflow_dq_agent to resolve from this checkout (make CORE=...)",
    )


def pytest_configure(config: pytest.Config) -> None:
    expected = Path(__file__).resolve().parents[1] / "src/ledgerlens/__init__.py"
    spec = importlib.util.find_spec("ledgerlens")
    origin = Path(spec.origin).resolve() if spec and spec.origin else None
    if origin is None or (
        not config.getoption("--installed-package") and origin != expected
    ):
        raise pytest.UsageError(
            f"Wrong checkout: expected {expected}, imported {origin}; "
            f"interpreter {sys.executable}. "
            "Install this checkout into its own environment."
        )
    core = importlib.util.find_spec("airflow_dq_agent")
    if core is None or core.origin is None:
        raise pytest.UsageError(
            "Missing airflow_dq_agent dependency; run make setup CORE=..."
        )
    selected = config.getoption("--core-checkout")
    if selected:
        expected_core = Path(selected).resolve() / "src/airflow_dq_agent/__init__.py"
        if Path(core.origin).resolve() != expected_core:
            raise pytest.UsageError(
                f"Wrong core checkout: expected {expected_core}, "
                f"imported {core.origin}; interpreter {sys.executable}. "
                f"Reinstall with make setup CORE={selected!r}."
            )


def pytest_sessionstart(session: pytest.Session) -> None:
    product = importlib.util.find_spec("ledgerlens")
    core = importlib.util.find_spec("airflow_dq_agent")
    revision = "unavailable"
    if core and core.origin and shutil.which("git"):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(Path(core.origin).parent),
                "rev-parse",
                "--show-toplevel",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            root, head = result.stdout.strip().splitlines()
            if (
                Path(root) / "src/airflow_dq_agent/__init__.py"
                == Path(core.origin).resolve()
            ):
                revision = head
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        mode = (
            "installed-package"
            if session.config.getoption("--installed-package")
            else "checkout"
        )
        reporter.write_line(
            f"Verification ({mode}): python={sys.executable}; "
            f"ledgerlens={product.origin if product else None}; "
            f"airflow_dq_agent={core.origin if core else None}; "
            f"core_revision={revision}"
        )


@pytest.fixture(scope="session")
def pg_dsns(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    from airflow_dq_agent.adoption import (
        apply_governance_schema,
        provision_restricted_logins,
    )

    from ledgerlens.core_integration import ensure_registered
    from ledgerlens.db import setup_database
    from ledgerlens.local_pg import LocalPostgres, find_free_port

    tmp = tmp_path_factory.mktemp("pg")
    pg = LocalPostgres(pgdata=tmp / "data", port=find_free_port())
    pg.start()
    try:
        pg.ensure_database("warehouse")
        admin_wh = pg.dsn(db="warehouse")
        apply_governance_schema(admin_wh)
        creds = provision_restricted_logins(admin_wh)
        intake = setup_database(admin_wh)
        traces = tmp_path_factory.mktemp("traces")
        os.environ["TRACE_POSTGRES"] = "true"
        os.environ["AUDIT_DSN"] = creds.audit_dsn
        os.environ["LLM_MODE"] = "stub"
        os.environ["APPLY_MODE"] = "off"
        os.environ["TRACES_DIR"] = str(traces)
        ensure_registered()
        yield {
            "admin": admin_wh,
            "intake": intake.intake_dsn,
            "read": creds.read_dsn,
            "audit": creds.audit_dsn,
        }
    finally:
        pg.stop()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset_db(pg_dsns: dict[str, str]) -> Iterator[None]:
    """Truncate test tables between tests. Test harness only; never runtime."""
    from airflow_dq_agent.warehouse.db import make_engine
    from sqlalchemy import text

    engine = make_engine(pg_dsns["admin"])
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE wb.case_verdicts, wb.cases, wb.submission_rows, "
                "wb.submissions, wb.staging_invoice, dq.check_runs, dq.traces"
            )
        )
    yield


@pytest.fixture()
def client(pg_dsns: dict[str, str]) -> TestClient:
    from ledgerlens.app import create_app
    from ledgerlens.settings import Settings

    settings = Settings(
        intake_dsn=pg_dsns["intake"],
        read_dsn=pg_dsns["read"],
        audit_dsn=pg_dsns["audit"],
    )
    return TestClient(create_app(settings))


@pytest.fixture()
def api_headers() -> dict[str, str]:
    return {"Accept": "application/json"}
