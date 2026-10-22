"""Deterministic extraction uses authored canonical text, never corpus labels."""

from __future__ import annotations

import hashlib

import pytest

from invoice_triage.deterministic import extract_fields
from invoice_triage.extraction import Success
from invoice_triage.pdf_text import ADAPTER, PDFText


@pytest.fixture(autouse=True)
def reset_db() -> None:
    """This pure text parser does not use PostgreSQL."""


def extract(*pages: str) -> Success:
    tagged = "".join(f"[page {number}]\n{page}" for number, page in enumerate(pages, 1))
    result = extract_fields(
        PDFText(
            version="1",
            status="text",
            pages=list(pages),
            text=tagged,
            text_sha256=hashlib.sha256(tagged.encode()).hexdigest(),
            adapter=ADAPTER,
        )
    )
    assert isinstance(result, Success)
    return result


def test_zero_total_ignores_subtotal_and_preserves_exact_page_offsets() -> None:
    page = (
        "Résumé\nInvoice-ID: Inv-0001\nSupplier ID: Sup-0042\n"
        "Invoice date: 2026-10-23\nSubtotal: 12.34 EUR\nGrand total: 0.00 EUR\n"
    )
    fields = extract("Cover page\n", page).fields
    assert {name: field.value for name, field in fields.items().items()} == {
        "invoice_id": "Inv-0001",
        "supplier_id": "Sup-0042",
        "invoice_date": "2026-10-23",
        "currency": "EUR",
        "amount_minor": 0,
    }
    assert all(field.state == "value" for field in fields.items().values())
    for field in fields.items().values():
        assert field.evidence
        for span in field.evidence:
            assert span.page == 2
            assert page[span.start : span.end].strip()
    amount_span = fields.amount_minor.evidence[0]
    assert page[amount_span.start : amount_span.end] == "0.00"
    identifier_span = fields.invoice_id.evidence[0]
    assert page[identifier_span.start : identifier_span.end] == "Inv-0001"


@pytest.mark.parametrize(
    "label", ["Invoice ID", "Invoice-ID", "Invoice number", "Invoice No.", "Invoice #"]
)
def test_identifier_labels_preserve_case_and_leading_zeroes(label: str) -> None:
    assert extract(f"{label}: Inv-0001\n").fields.invoice_id.value == "Inv-0001"


def test_absent_fields_have_no_fabricated_evidence_or_prose_currency() -> None:
    fields = extract("A different layout\nSubtotal: 12.34 EUR\nPay in USD.\n").fields
    for field in fields.items().values():
        assert field.state == "missing"
        assert field.value is None
        assert field.evidence == []


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Total: 1.00 EUR\nGrand total: 2.00 EUR\n", "amount_minor"),
        ("Currency: EUR\nTotal: 1.00 USD\n", "currency"),
        ("Invoice ID: A-01\nInvoice #: B-02\n", "invoice_id"),
        ("Supplier ID: A-01\nSupplier ID: B-02\n", "supplier_id"),
        ("Date: 2026-10-23\nInvoice date: 2026-10-24\n", "invoice_date"),
    ],
)
def test_conflicting_candidates_are_ambiguous_with_both_spans(
    text: str, name: str
) -> None:
    field = extract(text).fields.items()[name]
    assert field.state == "ambiguous"
    assert field.value is None
    assert len(field.evidence) == 2


def test_repeated_agreeing_candidates_across_pages_remain_definite() -> None:
    fields = extract(
        "Invoice ID: Inv-0001\nTotal: 1.00 EUR\n",
        "Invoice-ID: Inv-0001\nAmount due: 1.0 EUR\n",
    ).fields
    assert fields.invoice_id.value == "Inv-0001"
    assert fields.amount_minor.value == 100
    assert fields.currency.value == "EUR"
    assert [span.page for span in fields.amount_minor.evidence] == [1, 2]


@pytest.mark.parametrize(
    "amount",
    [
        "-1.00",
        "0.001",
        "1.000",
        "1,234.56",
        "1,00",
        "1e2",
        "NaN",
        "１２.３４",
        "",
        "92233720368547758.08",
        "9" * 5000,
    ],
)
def test_uninterpretable_money_is_ambiguous_without_rounding(amount: str) -> None:
    field = extract(f"Total: {amount}\n").fields.amount_minor
    assert field.state == "ambiguous"
    assert field.value is None
    assert field.evidence


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("0", 0),
        ("000.0", 0),
        ("12", 1200),
        ("12.3", 1230),
        ("12.34", 1234),
        ("92233720368547758.07", 9_223_372_036_854_775_807),
    ],
)
def test_money_uses_exact_integer_minor_units(amount: str, expected: int) -> None:
    assert extract(f"Invoice total: {amount}\n").fields.amount_minor.value == expected


@pytest.mark.parametrize(
    "value", ["2026-02-30", "23/10/2026", "20261023", "2026-W43-5", ""]
)
def test_bad_or_unsupported_date_stays_ambiguous(value: str) -> None:
    field = extract(f"Invoice date: {value}\n").fields.invoice_date
    assert field.state == "ambiguous"
    assert field.value is None
    assert field.evidence


@pytest.mark.parametrize("value", ["$", "GBP", "EUR USD", ""])
def test_unresolved_currency_is_ambiguous(value: str) -> None:
    field = extract(f"Currency: {value}\n").fields.currency
    assert field.state == "ambiguous"
    assert field.value is None
    assert field.evidence


@pytest.mark.parametrize("suffix", ["$", "GBP"])
def test_total_keeps_exact_money_but_refuses_unresolved_currency(suffix: str) -> None:
    fields = extract(f"Total: 1.00 {suffix}\n").fields
    assert fields.amount_minor.value == 100
    assert fields.currency.state == "ambiguous"
    assert fields.currency.value is None


@pytest.mark.parametrize(
    ("value", "expected"), [("EUR", "EUR"), ("usd", "USD"), ("€", "EUR")]
)
def test_supported_currency_label_and_total_suffix_have_exact_evidence(
    value: str, expected: str
) -> None:
    page = f"Currency: {value}\nTotal: 1.00 {value}\n"
    field = extract(page).fields.currency
    assert field.value == expected
    assert len(field.evidence) == 2
    assert all(page[span.start : span.end] == value for span in field.evidence)
