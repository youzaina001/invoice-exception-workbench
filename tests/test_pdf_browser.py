"""A real browser reviews, edits, confirms and submits a PDF against PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_browser_journey import live_server as live_server
from test_browser_journey import session_key as session_key
from test_evaluation import make_pdf
from test_saved_extraction import saved

INVOICE = (
    "Invoice ID: PDF-BROWSER\nSupplier ID: SUP-001\nInvoice date: 2026-10-23\n"
    "Currency: EUR\nInvoice total: 0.00\n"
)


def test_browser_imports_saved_output_with_visible_identity_and_confirmation(
    live_server: str,
) -> None:
    from invoice_triage.deterministic import extract_fields
    from invoice_triage.pdf_text import PDFText, read_pdf

    source = make_pdf([INVOICE])
    pdf = read_pdf(source)
    assert isinstance(pdf, PDFText)
    raw = extract_fields(pdf).model_dump(mode="json")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(java_script_enabled=False)
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(
                {
                    "name": "source.pdf",
                    "mimeType": "application/pdf",
                    "buffer": source,
                }
            )
            page.get_by_label("Saved extraction JSON (optional)").set_input_files(
                {
                    "name": "saved.json",
                    "mimeType": "application/json",
                    "buffer": saved(source, raw),
                }
            )
            page.get_by_role("button", name="Review PDF", exact=True).click()
            expect(
                page.get_by_text("Saved extraction: synthetic-model-stub", exact=False)
            ).to_be_visible()
            expect(
                page.get_by_role("button", name="Submit confirmed PDF")
            ).to_be_hidden()
            expect(
                page.get_by_label("Amount (decimal, for example 12.34)", exact=True)
            ).to_have_value("0.00")
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            page.get_by_role("button", name="Submit confirmed PDF").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_text("(no case opened)", exact=False)).to_be_visible()
        finally:
            browser.close()


@pytest.mark.parametrize("state", ["missing", "ambiguous"])
def test_pdf_absent_amount_correction_and_explicit_resolution_in_browser(
    live_server: str, state: str
) -> None:
    source = (
        INVOICE.replace("Invoice total: 0.00\n", "")
        if state == "missing"
        else INVOICE + "Invoice total: 99.00\n"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(java_script_enabled=False)
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(
                {
                    "name": f"{state}.pdf",
                    "mimeType": "application/pdf",
                    "buffer": make_pdf([source]),
                }
            )
            page.get_by_role("button", name="Review PDF", exact=True).click()
            original_url = page.url
            expect(
                page.get_by_label("Amount (decimal, for example 12.34) state")
            ).to_have_value(state)
            expect(
                page.get_by_text("Missing or ambiguous amounts can be submitted")
            ).to_be_visible()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            page.get_by_role("button", name="Submit confirmed PDF").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            case_url = page.url
            expect(page.get_by_role("heading", name="Failed check")).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()
            expect(page.get_by_text(state, exact=True).first).to_be_visible()

            page.goto(original_url)
            page.get_by_label("Replacement PDF", exact=True).set_input_files(
                {
                    "name": "corrected.pdf",
                    "mimeType": "application/pdf",
                    "buffer": make_pdf([INVOICE.replace("0.00", "12.34")]),
                }
            )
            page.get_by_role("button", name="Replace PDF source").click()
            expect(
                page.get_by_role("button", name="Submit confirmed PDF")
            ).to_be_hidden()
            expect(
                page.get_by_label("Amount (decimal, for example 12.34)", exact=True)
            ).to_have_value("12.34")
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            page.get_by_role("button", name="Submit confirmed PDF").click()
            page.goto(case_url)
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()
            expect(
                page.get_by_text("run checks on the current version first", exact=False)
            ).to_be_visible()
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(
                {
                    "name": "original-again.pdf",
                    "mimeType": "application/pdf",
                    "buffer": make_pdf([source]),
                }
            )
            page.get_by_role("button", name="Review PDF", exact=True).click()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            page.get_by_role("button", name="Submit confirmed PDF").click()
            expect(page.get_by_role("table").first.get_by_role("row")).to_have_count(3)
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_role("heading", name="Passed check")).to_be_visible()
            expect(
                page.get_by_role("heading", name="Case PDF-BROWSER — open")
            ).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_visible()
            page.get_by_role("button", name="Resolve case").click()
            expect(
                page.get_by_role("heading", name="Case PDF-BROWSER — resolved")
            ).to_be_visible()
            expect(page.get_by_role("button", name="Resolve case")).to_be_hidden()
            capture = os.environ.get("PDF_BROWSER_EVIDENCE")
            if capture:
                page.screenshot(
                    path=str(Path(capture) / f"{state}-resolved.png"), full_page=True
                )
        finally:
            browser.close()


@pytest.mark.parametrize("document", ["84b6c31d", "9d7f021b", "624ec9af", "71c54e03"])
def test_pdf_required_identity_refusal_in_browser(
    live_server: str, document: str
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures/extraction/development/pdfs"
        / f"{document}.pdf"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(java_script_enabled=False)
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(str(fixture))
            page.get_by_role("button", name="Review PDF", exact=True).click()
            draft_url = page.url
            saved = page.request.get(
                draft_url, headers={"Accept": "application/json"}
            ).json()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            expect(page.get_by_role("alert")).to_contain_text("must be definite")
            assert (
                page.request.get(
                    draft_url, headers={"Accept": "application/json"}
                ).json()
                == saved
            )
            assert (
                page.request.get(
                    live_server, headers={"Accept": "application/json"}
                ).json()["submissions"]
                == []
            )
        finally:
            browser.close()


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


@pytest.mark.parametrize("action", ["confirm", "intake"])
def test_unsaved_visible_fields_refuse_confirmation_and_intake(
    live_server: str, action: str
) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(java_script_enabled=False)
            page.goto(live_server)
            page.get_by_label("PDF file", exact=True).set_input_files(
                {
                    "name": "unsaved.pdf",
                    "mimeType": "application/pdf",
                    "buffer": make_pdf([INVOICE.replace("0.00", "12.34")]),
                }
            )
            page.get_by_role("button", name="Review PDF", exact=True).click()
            draft_url = page.url
            if action == "intake":
                page.get_by_role("button", name="Confirm reviewed PDF").click()
            saved = page.request.get(
                draft_url, headers={"Accept": "application/json"}
            ).json()
            page.get_by_label("Amount (decimal, for example 12.34)", exact=True).fill(
                "99.99"
            )
            button = (
                "Confirm reviewed PDF"
                if action == "confirm"
                else "Submit confirmed PDF"
            )
            with page.expect_response(
                lambda response: response.url == f"{draft_url}/{action}"
            ) as refused:
                page.get_by_role("button", name=button).click()
            assert refused.value.status == 409
            expect(page.get_by_role("alert")).to_contain_text("Save reviewed fields")
            assert (
                page.request.get(
                    draft_url, headers={"Accept": "application/json"}
                ).json()
                == saved
            )
            assert (
                page.request.get(
                    live_server, headers={"Accept": "application/json"}
                ).json()["submissions"]
                == []
            )

            page.goto(draft_url)
            page.get_by_label("Amount (decimal, for example 12.34)", exact=True).fill(
                "99.99"
            )
            page.get_by_role("button", name="Save reviewed fields").click()
            page.get_by_role("button", name="Confirm reviewed PDF").click()
            saved = page.request.get(
                draft_url, headers={"Accept": "application/json"}
            ).json()
            assert saved["fields"]["amount_minor"]["value"] == 9999
            page.get_by_role("button", name="Submit confirmed PDF").click()
            page.get_by_role("button", name="Run checks on current submission").click()
            expect(page.get_by_text("Last run", exact=False)).to_contain_text("pass")
        finally:
            browser.close()
