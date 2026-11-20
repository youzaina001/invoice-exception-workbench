"""Local PDF review: immutable sources/revisions and explicitly confirmed intake."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from invoice_triage.deterministic import extract_fields
from invoice_triage.extraction import (
    Fields,
    StrictModel,
    Success,
    canonical,
    digest,
    load_json,
    validate_fields,
    validate_result,
)
from invoice_triage.intake import InvoiceRow, ingest_invoice_row, parse_amount_minor
from invoice_triage.pdf_text import MAX_PDF_BYTES, PDFText, read_pdf
from invoice_triage.request_scope import ExecutionScope, scoped_transaction

MAX_SAVED_EXTRACTION_BYTES = 1024 * 1024

_FIELD_LABELS = {
    "invoice_id": "Invoice ID",
    "supplier_id": "Supplier ID",
    "invoice_date": "Invoice date",
    "currency": "Currency",
    "amount_minor": "Amount (decimal, for example 12.34)",
}


class _UnsetType:
    pass


_UNSET = _UnsetType()


class PDFConflict(ValueError):
    """The reviewed version or confirmation no longer matches the request."""


class _SavedExtraction(StrictModel):
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


def _require_local(scope: ExecutionScope | None) -> None:
    """Refuse hosted scopes before parsing input or opening a transaction."""
    if scope is not None and scope.hosted:
        raise PermissionError("PDF review is available locally only")


def _json(value: Any) -> str:
    return canonical(value).decode()


def _decode_saved_extraction(extraction: object) -> object:
    if isinstance(extraction, (bytes, bytearray)):
        if len(extraction) > MAX_SAVED_EXTRACTION_BYTES:
            raise ValueError("Saved extraction exceeds 1 MiB")
        decoded = load_json(bytes(extraction))
        if not isinstance(decoded, dict):
            raise ValueError("Saved extraction must be an object")
        return decoded
    return extraction


def _parse_revision(revision: int | None, payload: Mapping[str, Any] | None) -> int:
    value: object = (
        revision
        if revision is not None
        else payload.get("revision")
        if payload is not None
        else None
    )
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        value = int(value)
    if type(value) is not int:
        raise ValueError("A reviewed revision is required")
    return value


def _parse_binding(binding: str | None, payload: Mapping[str, Any] | None) -> str:
    value: object = (
        binding
        if binding is not None
        else payload.get("binding")
        if payload is not None
        else None
    )
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("A source and revision binding is required")
    return value


def _parse_form_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in Fields.model_fields:
        state = payload.get(f"{name}_state")
        value = payload.get(name)
        if not isinstance(value, str):
            raise ValueError("Every reviewed field is required")
        parsed: str | int | None = value.strip() or None
        if name == "amount_minor" and parsed is not None:
            parsed = parse_amount_minor(str(parsed), filename="Reviewed PDF")
        fields[name] = {"state": state, "value": parsed, "evidence": []}
    return fields


def _parse_edit_fields(
    fields: object, payload: Mapping[str, Any] | None, *, form: bool
) -> object:
    if not isinstance(fields, _UnsetType):
        return fields
    if payload is None:
        raise ValueError("Every reviewed field is required")
    if form:
        return _parse_form_fields(payload)
    if "fields" in payload:
        return payload["fields"]
    return _parse_form_fields(payload)


def _parse_reviewed_fields(
    reviewed_fields: object, payload: Mapping[str, Any] | None, *, form: bool
) -> object | None:
    if not isinstance(reviewed_fields, _UnsetType):
        return reviewed_fields
    if payload is None:
        return None
    if form:
        return _parse_form_fields(payload)
    return payload.get("fields")


def _binding(
    draft: str,
    revision: int,
    source_hash: str,
    fields: Any,
    extraction: Any = None,
) -> str:
    return digest(
        canonical(
            {
                "draft": draft,
                "revision": revision,
                "source": source_hash,
                "fields": fields,
                "owner": "local",
                **({"extraction": extraction} if extraction is not None else {}),
            }
        )
    )


def _source(data: bytes, filename: str) -> dict[str, Any]:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the 5 MiB limit")
    converted = read_pdf(data)
    raw = extract_fields(converted) if isinstance(converted, PDFText) else converted
    return {
        "id": uuid4().hex,
        "source_hash": digest(data),
        "data": data,
        "filename": filename,
        "pages": converted.pages if isinstance(converted, PDFText) else [],
        "raw": raw.model_dump(mode="json"),
    }


def _store_source(conn: Connection, source: dict[str, Any]) -> dict[str, Any]:
    conn.execute(
        text(
            "INSERT INTO wb.pdf_sources (id, source_hash, data, filename, pages, raw) "
            "VALUES (:id, :source_hash, :data, :filename, CAST(:pages AS JSONB), "
            "CAST(:raw AS JSONB)) ON CONFLICT (source_hash) DO NOTHING"
        ),
        {**source, "pages": _json(source["pages"]), "raw": _json(source["raw"])},
    )
    return dict(
        conn.execute(
            text(
                "SELECT id, source_hash, filename, pages, raw FROM wb.pdf_sources "
                "WHERE source_hash = :hash"
            ),
            {"hash": source["source_hash"]},
        )
        .mappings()
        .one()
    )


def _read(conn: Connection, draft_id: str, *, lock: bool = False) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT d.id, d.revision, r.source_id, r.fields, r.binding, "
                "s.source_hash, s.filename, s.pages, r.extraction, "
                "COALESCE(r.extraction->'result', s.raw) AS raw, "
                "c.binding AS confirmation, i.submission_id AS submitted_id "
                "FROM wb.pdf_drafts d JOIN wb.pdf_revisions r "
                "ON r.draft_id = d.id AND r.revision = d.revision "
                "JOIN wb.pdf_sources s ON s.id = r.source_id "
                "LEFT JOIN wb.pdf_confirmations c "
                "ON c.draft_id = d.id AND c.revision = d.revision "
                "LEFT JOIN wb.pdf_intakes i "
                "ON i.draft_id = d.id AND i.revision = d.revision "
                "WHERE d.id = :id AND d.owner_context = 'local'"
                + (" FOR UPDATE OF d" if lock else "")
            ),
            {"id": draft_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise LookupError("PDF draft not found")
    result = dict(row)
    result["confirmed"] = result.pop("confirmation") == result["binding"]
    return result


def _revision(
    conn: Connection,
    draft_id: str,
    revision: int,
    source: dict[str, Any],
    fields: dict[str, Any] | None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO wb.pdf_revisions "
            "(draft_id, revision, source_id, fields, binding, extraction) "
            "VALUES (:draft, :revision, :source, CAST(:fields AS JSONB), :binding, "
            "CAST(:extraction AS JSONB))"
        ),
        {
            "draft": draft_id,
            "revision": revision,
            "source": source["id"],
            "fields": _json(fields),
            "binding": _binding(
                draft_id,
                revision,
                source["source_hash"],
                fields,
                source.get("extraction"),
            ),
            "extraction": (
                _json(source["extraction"])
                if source.get("extraction") is not None
                else None
            ),
        },
    )
    conn.execute(
        text("UPDATE wb.pdf_drafts SET revision = :revision WHERE id = :id"),
        {"revision": revision, "id": draft_id},
    )


def _extracted_fields(source: dict[str, Any]) -> dict[str, Any] | None:
    result = validate_result(source["raw"], source["pages"])
    return (
        result.fields.model_dump(mode="json") if isinstance(result, Success) else None
    )


def create_draft(
    engine: Engine,
    *,
    filename: str,
    data: bytes,
    extraction: object = None,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    """Store a local PDF source and create its initial review draft."""
    _require_local(scope)
    parsed_extraction = (
        _decode_saved_extraction(extraction) if extraction is not None else None
    )
    source = _source(data, filename)
    saved = None
    if parsed_extraction is not None:
        if source["raw"]["status"] != "ok":
            raise ValueError("PDF adapter or document refusal cannot be overridden")
        saved = _SavedExtraction.model_validate(parsed_extraction).checked(
            source["source_hash"], source["pages"]
        )
    draft_id = uuid4().hex
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        stored = _store_source(conn, source)
        if saved is not None:
            stored.update(extraction=saved, raw=saved["result"])
        conn.execute(
            text(
                "INSERT INTO wb.pdf_drafts (id, owner_context, revision) "
                "VALUES (:id, 'local', 1)"
            ),
            {"id": draft_id},
        )
        _revision(conn, draft_id, 1, stored, _extracted_fields(stored))
        return _read(conn, draft_id)


def read_draft(
    engine: Engine, draft_id: str, *, scope: ExecutionScope | None = None
) -> dict[str, Any]:
    """Read the current revision and confirmation state of a local PDF draft."""
    with scoped_transaction(engine, scope, mutate=False, local_only=True) as conn:
        return _read(conn, draft_id)


def read_source(
    engine: Engine, draft_id: str, *, scope: ExecutionScope | None = None
) -> bytes:
    """Read raw PDF bytes for the active revision of a local draft."""
    with scoped_transaction(engine, scope, mutate=False, local_only=True) as conn:
        draft = _read(conn, draft_id)
        return bytes(
            conn.execute(
                text("SELECT data FROM wb.pdf_sources WHERE id = :id"),
                {"id": draft["source_id"]},
            ).scalar_one()
        )


def _current(
    draft: dict[str, Any],
    revision: int,
    binding: str | None = None,
    reviewed_fields: object | None = None,
) -> None:
    if revision != draft["revision"] or (
        binding is not None and binding != draft["binding"]
    ):
        raise PDFConflict(
            "PDF source or reviewed revision changed; review and confirm again"
        )
    if reviewed_fields is not None:
        if draft["fields"] is None:
            raise ValueError("Replace the refused PDF source before reviewing fields")
        reviewed = validate_fields(reviewed_fields, draft["pages"])
        for name, field in reviewed.items().items():
            saved = draft["fields"][name]
            if field.state != saved["state"] or field.value != saved["value"]:
                raise PDFConflict("Save reviewed fields, then review and confirm again")


def edit_draft(
    engine: Engine,
    draft_id: str,
    *,
    revision: int | None = None,
    fields: object = _UNSET,
    payload: Mapping[str, Any] | None = None,
    form: bool = False,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    """Record a new reviewed revision for a local PDF draft."""
    _require_local(scope)
    resolved_revision = _parse_revision(revision, payload)
    resolved_fields = _parse_edit_fields(fields, payload, form=form)
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, resolved_revision)
        if draft["raw"]["status"] != "ok":
            raise ValueError("Replace the refused PDF source before reviewing fields")
        reviewed = validate_fields(resolved_fields, draft["pages"])
        source = {
            "id": draft["source_id"],
            "source_hash": draft["source_hash"],
            "extraction": draft["extraction"],
        }
        _revision(
            conn,
            draft_id,
            resolved_revision + 1,
            source,
            reviewed.model_dump(mode="json"),
        )
        return _read(conn, draft_id)


def replace_source(
    engine: Engine,
    draft_id: str,
    *,
    revision: int | None = None,
    filename: str,
    data: bytes,
    payload: Mapping[str, Any] | None = None,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    """Attach a replacement PDF source to a local draft as a new revision."""
    _require_local(scope)
    resolved_revision = _parse_revision(revision, payload)
    source = _source(data, filename)
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, resolved_revision)
        stored = _store_source(conn, source)
        _revision(
            conn, draft_id, resolved_revision + 1, stored, _extracted_fields(stored)
        )
        return _read(conn, draft_id)


def _validated(draft: dict[str, Any]) -> Fields:
    fields = validate_fields(draft["fields"], draft["pages"])
    if any(
        field.state != "value"
        for name, field in fields.items().items()
        if name != "amount_minor"
    ):
        raise ValueError("Invoice ID, supplier ID, date and currency must be definite")
    return fields


def _verify_source(conn: Connection, draft: dict[str, Any]) -> None:
    data = bytes(
        conn.execute(
            text("SELECT data FROM wb.pdf_sources WHERE id = :id"),
            {"id": draft["source_id"]},
        ).scalar_one()
    )
    expected = _binding(
        draft["id"],
        draft["revision"],
        digest(data),
        draft["fields"],
        draft["extraction"],
    )
    if expected != draft["binding"]:
        raise PDFConflict(
            "PDF source or reviewed fields changed; review and confirm again"
        )


def confirm_draft(
    engine: Engine,
    draft_id: str,
    *,
    revision: int | None = None,
    binding: str | None = None,
    reviewed_fields: object = _UNSET,
    payload: Mapping[str, Any] | None = None,
    form: bool = False,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    """Explicitly confirm the active PDF source and reviewed fields."""
    _require_local(scope)
    resolved_revision = _parse_revision(revision, payload)
    resolved_binding = _parse_binding(binding, payload)
    resolved_reviewed = _parse_reviewed_fields(reviewed_fields, payload, form=form)
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, resolved_revision, resolved_binding, resolved_reviewed)
        _validated(draft)
        _verify_source(conn, draft)
        conn.execute(
            text(
                "INSERT INTO wb.pdf_confirmations (draft_id, revision, binding) "
                "VALUES (:id, :revision, :binding) "
                "ON CONFLICT (draft_id, revision) DO NOTHING"
            ),
            {
                "id": draft_id,
                "revision": resolved_revision,
                "binding": resolved_binding,
            },
        )
        return _read(conn, draft_id)


def intake_draft(
    engine: Engine,
    draft_id: str,
    *,
    revision: int | None = None,
    binding: str | None = None,
    reviewed_fields: object = _UNSET,
    payload: Mapping[str, Any] | None = None,
    form: bool = False,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    """Submit a confirmed PDF draft into submission history and singleton staging."""
    _require_local(scope)
    resolved_revision = _parse_revision(revision, payload)
    resolved_binding = _parse_binding(binding, payload)
    resolved_reviewed = _parse_reviewed_fields(reviewed_fields, payload, form=form)
    with scoped_transaction(
        engine, scope, mutate=True, lock_staging=False, local_only=True
    ) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, resolved_revision, resolved_binding, resolved_reviewed)
        if not draft["confirmed"]:
            raise PDFConflict("Explicit confirmation is required before PDF intake")
        fields = _validated(draft)
        _verify_source(conn, draft)
        assert fields.invoice_id.value is not None
        assert fields.supplier_id.value is not None
        assert fields.invoice_date.value is not None
        assert fields.currency.value is not None
        row = InvoiceRow(
            fields.invoice_id.value,
            fields.supplier_id.value,
            date.fromisoformat(fields.invoice_date.value),
            fields.currency.value,
            fields.amount_minor.value,
        )
        # Replacement PDFs retain distinct submission history.
        content_hash = digest(
            canonical(
                {
                    "source": draft["source_hash"],
                    "fields": {
                        name: {"state": field.state, "value": field.value}
                        for name, field in fields.items().items()
                    },
                }
            )
        )
        result = ingest_invoice_row(
            conn,
            filename=draft["filename"],
            row=row,
            content_hash=content_hash,
            amount_state=fields.amount_minor.state,
            scope=scope,
        )
        conn.execute(
            text(
                "INSERT INTO wb.pdf_intakes (draft_id, revision, submission_id) "
                "VALUES (:id, :revision, :submission) "
                "ON CONFLICT (draft_id, revision) DO NOTHING"
            ),
            {
                "id": draft_id,
                "revision": resolved_revision,
                "submission": result["submission_id"],
            },
        )
        return result


def review_rows(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Format reviewed PDF fields, cent strings, and sliced source evidence."""
    rows: list[dict[str, Any]] = []
    for name, field in (draft["fields"] or {}).items():
        value = field["value"]
        if name == "amount_minor" and value is not None:
            value = f"{value // 100}.{value % 100:02d}"
        rows.append(
            {
                "name": name,
                "label": _FIELD_LABELS[name],
                "state": field["state"],
                "value": value if value is not None else "",
                "evidence": [
                    {
                        "page": span["page"],
                        "text": draft["pages"][span["page"] - 1][
                            span["start"] : span["end"]
                        ],
                    }
                    for span in draft["raw"]["fields"][name]["evidence"]
                ],
            }
        )
    return rows
