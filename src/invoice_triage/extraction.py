"""The five-field result shared by offline extraction arms."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

State = Literal["value", "missing", "ambiguous"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Span(StrictModel):
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class FieldResult[T](StrictModel):
    state: State
    value: T | None
    evidence: list[Span]

    @model_validator(mode="after")
    def check_state(self) -> FieldResult[T]:
        if (self.state == "value") != (self.value is not None):
            raise ValueError("value state requires a value; absent states require null")
        if self.state == "missing" and self.evidence:
            raise ValueError("missing fields cannot have a fabricated locator")
        return self


class Fields(StrictModel):
    invoice_id: FieldResult[str]
    supplier_id: FieldResult[str]
    invoice_date: FieldResult[str]
    currency: FieldResult[str]
    amount_minor: FieldResult[int]

    @model_validator(mode="after")
    def normalize(self) -> Fields:
        for field in (self.invoice_id, self.supplier_id):
            if field.value is not None:
                field.value = field.value.strip()
                if not field.value:
                    raise ValueError("an identifier value cannot be empty")
        if self.invoice_date.value is not None:
            value = self.invoice_date.value.strip()
            if re.fullmatch(r"\d{4}-?W\d{2}", value):
                raise ValueError("an invoice week date requires a weekday")
            self.invoice_date.value = date.fromisoformat(value).isoformat()
        if self.currency.value is not None:
            self.currency.value = self.currency.value.strip().upper()
            if self.currency.value not in {"EUR", "USD"}:
                raise ValueError("currency values must be EUR or USD")
        amount = self.amount_minor.value
        if amount is not None and not 0 <= amount <= 9_223_372_036_854_775_807:
            raise ValueError("amount is outside the supported range")
        return self

    def items(self) -> dict[str, FieldResult[str] | FieldResult[int]]:
        return {
            "invoice_id": self.invoice_id,
            "supplier_id": self.supplier_id,
            "invoice_date": self.invoice_date,
            "currency": self.currency,
            "amount_minor": self.amount_minor,
        }


class Success(StrictModel):
    version: Literal["1"]
    status: Literal["ok"]
    fields: Fields


class Failure(StrictModel):
    version: Literal["1"]
    status: Literal["failure"]
    stage: Literal["pdf", "extract", "validate"]
    code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")


Result = Annotated[Success | Failure, Field(discriminator="status")]
RESULT = TypeAdapter(Result)


def canonical(value: Any) -> bytes:
    """Serialize JSON deterministically without NaN or Infinity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def read_artifact(root: Path, name: str) -> bytes:
    """Read a bundle-contained non-secret artifact up to 8 MiB."""
    if any(p.startswith(".env") or p.endswith(".pem") for p in Path(name).parts):
        raise ValueError("environment and key files are not replay artifacts")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("artifact path leaves its bundle")
    with path.open("rb") as source:
        data = source.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("artifact exceeds 8 MiB")
    return data


def load_json(data: bytes) -> Any:
    """Decode strict JSON rejecting duplicate keys and nonstandard constants."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        data, object_pairs_hook=unique_keys, parse_constant=reject_constant
    )


def _check_evidence_spans(fields: Fields, pages: Sequence[str]) -> None:
    for field in fields.items().values():
        for span in field.evidence:
            if not (
                1 <= span.page <= len(pages)
                and span.start < span.end <= len(pages[span.page - 1])
            ):
                raise ValueError("evidence span is outside the canonical page text")


def validate_fields(raw: object, pages: Sequence[str] = ()) -> Fields:
    """Validate and normalize the five reviewed invoice fields against page spans."""
    fields = Fields.model_validate(raw)
    _check_evidence_spans(fields, pages)
    return fields


def validate_result(raw: object, pages: Sequence[str]) -> Success | Failure:
    result = RESULT.validate_python(raw)
    if isinstance(result, Success):
        _check_evidence_spans(result.fields, pages)
    return result
