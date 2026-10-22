"""The five-field result shared by offline extraction arms."""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

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


def validate_result(raw: object, pages: list[str]) -> Success | Failure:
    result = RESULT.validate_python(raw)
    if isinstance(result, Success):
        for field in result.fields.items().values():
            for span in field.evidence:
                if not (
                    1 <= span.page <= len(pages)
                    and span.start < span.end <= len(pages[span.page - 1])
                ):
                    raise ValueError("evidence span is outside the canonical page text")
    return result
