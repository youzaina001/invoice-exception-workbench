"""M1 browser journey: real Chromium drives the operator flow on a live server."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from ledgerlens.local_pg import find_free_port

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="module")
def live_server(
    pg_dsns: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    port = find_free_port()
    work = tmp_path_factory.mktemp("uvicorn")
    log = work / "server.log"
    env = {
        **os.environ,
        "WAREHOUSE_DSN": pg_dsns["read"],
        "INTAKE_DSN": pg_dsns["intake"],
        "READ_DSN": pg_dsns["read"],
        "AUDIT_DSN": pg_dsns["audit"],
        "LLM_MODE": "stub",
        "APPLY_MODE": "off",
        "TRACE_POSTGRES": "true",
        "TRACES_DIR": str(tmp_path_factory.mktemp("ltraces")),
    }
    with open(log, "w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                str(ROOT / ".venv" / "bin" / "uvicorn"),
                "ledgerlens.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if httpx.get(base, timeout=2).status_code == 200:
                    break
            except httpx.ConnectError:
                time.sleep(0.5)
        else:
            raise RuntimeError(
                f"server never came up; log tail: {log.read_text()[-2000:]}"
            )
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def test_operator_journey_in_browser(live_server: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(live_server)
            expect(page.get_by_role("heading", name="LedgerLens")).to_be_visible()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_role("alert")).to_contain_text("no submission staged")
            page.get_by_role(
                "link", name="Return to submissions and run checks"
            ).click()

            page.locator('input[type="file"]').set_input_files(
                str(FIXTURES / "missing_amount.csv")
            )
            page.get_by_role("button", name="Ingest").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_text("INV-001").first).to_be_visible()
            expect(page.get_by_text("missing").first).to_be_visible()
            expect(page.get_by_text("unknown").first).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()
            expect(
                page.get_by_role("heading", name="Core failure samples")
            ).to_be_visible()

            page.goto(live_server)
            page.locator('input[type="file"]').set_input_files(
                str(FIXTURES / "corrected_amount.csv")
            )
            page.get_by_role("button", name="Ingest").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_text("12345").first).to_be_visible()
            expect(page.get_by_role("heading", name="Passed check")).to_be_visible()

            page.get_by_role("button", name="Resolve case").click()
            expect(page.get_by_text("resolved").first).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()

            page.goto(live_server)
            page.locator('input[type="file"]').set_input_files(
                str(FIXTURES / "corrected_amount.csv")
            )
            page.get_by_role("button", name="Ingest").click()
            expect(page.get_by_text("Duplicate upload:")).to_contain_text(
                "restaged as current"
            )
        finally:
            browser.close()
