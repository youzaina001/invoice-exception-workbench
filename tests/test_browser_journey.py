"""M1 browser journey: real Chromium drives the operator flow on a live server."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import expect, sync_playwright

from ledgerlens.local_pg import find_free_port

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="module")
def live_server(
    pg_dsns: dict[str, str],
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
    session_key: Any,
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
    if getattr(request, "param", False):
        env.update(
            HOSTED="true",
            CLERK_ISSUER="https://test.clerk.accounts.dev",
            CLERK_AUTHORIZED_PARTIES=f'["https://demo.example.com", "http://127.0.0.1:{port}"]',
            CLERK_JWT_KEY=session_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode(),
        )
    with open(log, "w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            ["make", "--no-print-directory", "run", f"PORT={port}"],
            start_new_session=True,
            cwd=ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise RuntimeError(f"server exited: {log.read_text()[-2000:]}")
            try:
                if httpx.get(base, timeout=2).status_code in (200, 401):
                    break
            except httpx.ConnectError:
                time.sleep(0.5)
        else:
            raise RuntimeError(
                f"server never came up; log tail: {log.read_text()[-2000:]}"
            )
        yield base
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
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
            expect(page.get_by_text("Unknown").first).to_be_visible()
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


@pytest.fixture(scope="module")
def session_key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.mark.parametrize("live_server", [True], indirect=True)
def test_hosted_attempt_history_in_browser(live_server: str, session_key: Any) -> None:
    token = jwt.encode(
        {
            "sub": "browser-reviewer",
            "sid": "sess_browser",
            "iss": "https://test.clerk.accounts.dev",
            "azp": "https://demo.example.com",
            "exp": int(time.time()) + 120,
            "nbf": int(time.time()) - 5,
            "iat": int(time.time()) - 5,
        },
        session_key,
        algorithm="RS256",
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context()
            context.add_cookies(
                [{"name": "__session", "value": token, "url": live_server}]
            )
            page = context.new_page()
            page.goto(live_server)
            page.locator('input[type="file"]').set_input_files(
                str(FIXTURES / "missing_amount.csv")
            )
            page.get_by_role("button", name="Ingest").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            page.get_by_role("link", name="Current Demo Attempt").click()
            page.get_by_role("button", name="Start fresh", exact=True).click()
            expect(page.get_by_text("missing_amount.csv", exact=True)).to_be_hidden()
            page.locator('input[type="file"]').set_input_files(
                {"name": "invalid.csv", "mimeType": "text/csv", "buffer": b"bad,csv\n"}
            )
            page.get_by_role("button", name="Ingest", exact=True).click()
            expect(page.locator(".error")).to_be_visible()
            expect(
                page.get_by_role("button", name="Start fresh", exact=True)
            ).to_be_visible()
            page.get_by_role("link", name="Attempt from").click()
            expect(
                page.get_by_role("heading", name="Read-only Demo Attempt")
            ).to_be_visible()
            expect(page.get_by_text("missing_amount.csv", exact=True)).to_be_visible()
            expect(page.get_by_role("heading", name="Audit evidence")).to_be_visible()
            expect(page.get_by_role("button", name="Ingest", exact=True)).to_be_hidden()
            page.get_by_role("link", name="INV-001", exact=True).click()
            expect(page.get_by_text("This Demo Attempt is read-only.")).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()
        finally:
            browser.close()
