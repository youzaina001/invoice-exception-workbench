"""A real browser reviews, edits, confirms and submits a PDF against PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from test_browser_journey import live_server as live_server
from test_browser_journey import session_key as session_key
from test_evaluation import make_pdf

INVOICE = (
    "Invoice ID: PDF-BROWSER\nSupplier ID: SUP-001\nInvoice date: 2026-10-23\n"
    "Currency: EUR\nInvoice total: 0.00\n"
)


def test_pdf_review_confirmation_and_fresh_check_in_browser(live_server: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(
                {
                    "name": "browser.pdf",
                    "mimeType": "application/pdf",
                    "buffer": make_pdf([INVOICE]),
                }
            )
            page.get_by_role("button", name="Review PDF", exact=True).click()
            expect(page.get_by_role("heading", name="Original source")).to_be_visible()
            expect(
                page.get_by_label("Amount (decimal, for example 12.34)", exact=True)
            ).to_have_value("0.00")
            expect(
                page.get_by_role("button", name="Submit confirmed PDF")
            ).to_be_hidden()
            page.get_by_label("Invoice ID", exact=True).fill("PDF-REVIEWED")
            page.get_by_role("button", name="Save reviewed fields").click()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            expect(
                page.get_by_text("This source and reviewed revision are confirmed.")
            ).to_be_visible()
            page.reload()
            expect(
                page.get_by_role("button", name="Submit confirmed PDF")
            ).to_be_visible()

            # Saving a later revision requires a new explicit confirmation.
            page.get_by_role("button", name="Save reviewed fields").click()
            expect(
                page.get_by_role("button", name="Submit confirmed PDF")
            ).to_be_hidden()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            source = page.locator("pre").first
            expect(source).to_contain_text("PDF-BROWSER")
            expect(page.get_by_label("Invoice ID", exact=True)).to_have_value(
                "PDF-REVIEWED"
            )
            capture = os.environ.get("PDF_BROWSER_EVIDENCE")
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                )
                if capture:
                    page.screenshot(
                        path=str(Path(capture) / f"pdf-review-{width}.png"),
                        full_page=True,
                    )
            page.get_by_role("button", name="Submit confirmed PDF").click()
            expect(page.get_by_text("PDF-REVIEWED", exact=True)).to_be_visible()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_text("(no case opened)", exact=False)).to_be_visible()
            expect(page.get_by_text("Last run", exact=False)).to_contain_text("pass")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if capture:
                page.screenshot(
                    path=str(Path(capture) / "pdf-passed.png"), full_page=True
                )
        finally:
            browser.close()
