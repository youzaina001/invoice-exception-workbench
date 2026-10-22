"""Source-bound saved output; metadata is a claim, not provider verification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from invoice_triage.extraction import StrictModel, validate_result


class SavedExtraction(StrictModel):
    version: Literal["1"]
    kind: Literal["offline-stub", "controlled-transport", "saved-provider-output"]
    arm: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: Any

    def checked(self, source_hash: str, pages: list[str]) -> dict[str, Any]:
        if self.source_sha256 != source_hash:
            raise ValueError("Saved extraction source hash differs from the PDF")
        result = validate_result(self.result, pages)
        return {
            **self.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        }
