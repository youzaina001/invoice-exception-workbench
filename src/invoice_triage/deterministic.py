"""Small deterministic rules for labelled, digital-text invoice fields."""

from __future__ import annotations

import re
from datetime import date

from invoice_triage.extraction import Failure, Span, Success, validate_result
from invoice_triage.pdf_text import INVOICE_ID, PDFText

LABELS = {
    "supplier_id": r"supplier[ \t-]+id",
    "invoice_date": r"(?:invoice[ \t]+)?date",
    "currency": r"currency",
    "amount_minor": r"(?:invoice[ \t]+total|grand[ \t]+total|amount[ \t]+due|total)",
}
RULES = {
    "invoice_id": INVOICE_ID,
    **{
        name: re.compile(
            rf"^[ \t]*{label}[ \t]*(?::[ \t]*|[ \t]+)(?P<value>.*?)[ \t]*$",
            re.IGNORECASE,
        )
        for name, label in LABELS.items()
    },
}
CURRENCY_SUFFIX = re.compile(r"(?P<currency>[A-Za-z]{3}|€|\$)$")


def currency_value(raw: str) -> str | None:
    value = raw.upper()
    return "EUR" if value == "€" else value if value in {"EUR", "USD"} else None


def amount_value(raw: str) -> int | None:
    # shortcut: decimal dots only; add locale formats with explicit ambiguity rules.
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw):
        return None
    major, _, fraction = raw.partition(".")
    major = major.lstrip("0") or "0"
    if len(major) > 17:
        return None
    value = int(major) * 100 + int(fraction.ljust(2, "0"))
    return value if value <= 9_223_372_036_854_775_807 else None


def extract_fields(pdf: PDFText) -> Success | Failure:
    candidates: dict[str, list[tuple[str | int | None, Span]]] = {
        name: [] for name in RULES
    }
    for page_number, page in enumerate(pdf.pages, 1):
        offset = 0
        for original in page.splitlines(keepends=True):
            line = original.rstrip("\r\n")
            for name, rule in RULES.items():
                match = rule.fullmatch(line)
                if match is None:
                    continue
                raw = match["value"]
                start, end = match.span("value")
                if name == "amount_minor":
                    suffix = CURRENCY_SUFFIX.search(raw)
                    if suffix is not None:
                        currency_span = Span(
                            page=page_number,
                            start=offset + start + suffix.start(),
                            end=offset + start + suffix.end(),
                        )
                        candidates["currency"].append(
                            (currency_value(suffix["currency"]), currency_span)
                        )
                        raw = raw[: suffix.start()].rstrip(" \t")
                        end = start + len(raw)
                span = Span(
                    page=page_number,
                    start=offset + (start if raw else match.start()),
                    end=offset + (end if raw else match.end()),
                )
                value: str | int | None = raw or None
                if name == "currency":
                    value = currency_value(raw)
                elif name == "amount_minor":
                    value = amount_value(raw)
                elif name == "invoice_date":
                    try:
                        value = (
                            date.fromisoformat(raw).isoformat()
                            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw)
                            else None
                        )
                    except ValueError:
                        value = None
                candidates[name].append((value, span))
                break
            offset += len(original)

    fields: dict[str, object] = {}
    for name, found in candidates.items():
        values = {value for value, _ in found}
        definite = len(values) == 1 and None not in values
        fields[name] = {
            "state": "value" if definite else "ambiguous" if found else "missing",
            "value": next(iter(values)) if definite else None,
            "evidence": [span.model_dump() for _, span in found],
        }
    return validate_result(
        {"version": "1", "status": "ok", "fields": fields}, pdf.pages
    )
