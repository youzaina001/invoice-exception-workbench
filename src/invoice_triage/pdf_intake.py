"""Local PDF review: immutable sources/revisions and explicitly confirmed intake."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from invoice_triage.deterministic import extract_fields
from invoice_triage.extraction import Fields, Success, validate_result
from invoice_triage.intake import InvoiceRow, ingest_invoice_row
from invoice_triage.pdf_text import MAX_PDF_BYTES, PDFText, read_pdf
from invoice_triage.request_scope import ExecutionScope, scoped_transaction
from invoice_triage.saved_extraction import SavedExtraction


class PDFConflict(ValueError):
    """The reviewed version or confirmation no longer matches the request."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _binding(
    draft: str,
    revision: int,
    source_hash: str,
    fields: Any,
    extraction: Any = None,
) -> str:
    return hashlib.sha256(
        _json(
            {
                "draft": draft,
                "revision": revision,
                "source": source_hash,
                "fields": fields,
                "owner": "local",
                **({"extraction": extraction} if extraction is not None else {}),
            }
        ).encode()
    ).hexdigest()


def _source(data: bytes, filename: str) -> dict[str, Any]:
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the 5 MiB limit")
    converted = read_pdf(data)
    raw = extract_fields(converted) if isinstance(converted, PDFText) else converted
    return {
        "id": uuid4().hex,
        "source_hash": hashlib.sha256(data).hexdigest(),
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
    if scope is not None and scope.hosted:
        raise PermissionError("PDF review is available locally only")
    source = _source(data, filename)
    saved = None
    if extraction is not None:
        if source["raw"]["status"] != "ok":
            raise ValueError("PDF adapter or document refusal cannot be overridden")
        saved = SavedExtraction.model_validate(extraction).checked(
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
    with scoped_transaction(engine, scope, mutate=False, local_only=True) as conn:
        return _read(conn, draft_id)


def read_source(
    engine: Engine, draft_id: str, *, scope: ExecutionScope | None = None
) -> bytes:
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
        reviewed = validate_result(
            {"version": "1", "status": "ok", "fields": reviewed_fields}, draft["pages"]
        )
        assert isinstance(reviewed, Success)
        for name, field in reviewed.fields.items().items():
            saved = draft["fields"][name]
            if field.state != saved["state"] or field.value != saved["value"]:
                raise PDFConflict("Save reviewed fields, then review and confirm again")


def edit_draft(
    engine: Engine,
    draft_id: str,
    *,
    revision: int,
    fields: object,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, revision)
        if draft["raw"]["status"] != "ok":
            raise ValueError("Replace the refused PDF source before reviewing fields")
        reviewed = validate_result(
            {"version": "1", "status": "ok", "fields": fields}, draft["pages"]
        )
        assert isinstance(reviewed, Success)
        source = {
            "id": draft["source_id"],
            "source_hash": draft["source_hash"],
            "extraction": draft["extraction"],
        }
        _revision(
            conn,
            draft_id,
            revision + 1,
            source,
            reviewed.fields.model_dump(mode="json"),
        )
        return _read(conn, draft_id)


def replace_source(
    engine: Engine,
    draft_id: str,
    *,
    revision: int,
    filename: str,
    data: bytes,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    if scope is not None and scope.hosted:
        raise PermissionError("PDF review is available locally only")
    source = _source(data, filename)
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, revision)
        stored = _store_source(conn, source)
        _revision(conn, draft_id, revision + 1, stored, _extracted_fields(stored))
        return _read(conn, draft_id)


def _validated(draft: dict[str, Any]) -> Fields:
    result = validate_result(
        {"version": "1", "status": "ok", "fields": draft["fields"]}, draft["pages"]
    )
    assert isinstance(result, Success)
    if any(
        field.state != "value"
        for name, field in result.fields.items().items()
        if name != "amount_minor"
    ):
        raise ValueError("Invoice ID, supplier ID, date and currency must be definite")
    return result.fields


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
        hashlib.sha256(data).hexdigest(),
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
    revision: int,
    binding: str,
    reviewed_fields: object | None = None,
    scope: ExecutionScope | None = None,
) -> dict[str, Any]:
    with scoped_transaction(engine, scope, mutate=True, local_only=True) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, revision, binding, reviewed_fields)
        _validated(draft)
        _verify_source(conn, draft)
        conn.execute(
            text(
                "INSERT INTO wb.pdf_confirmations (draft_id, revision, binding) "
                "VALUES (:id, :revision, :binding) "
                "ON CONFLICT (draft_id, revision) DO NOTHING"
            ),
            {"id": draft_id, "revision": revision, "binding": binding},
        )
        return _read(conn, draft_id)


def intake_draft(
    engine: Engine,
    draft_id: str,
    *,
    revision: int,
    binding: str,
    reviewed_fields: object | None = None,
    scope: ExecutionScope | None = None,
) -> dict[str, object]:
    with scoped_transaction(
        engine, scope, mutate=True, lock_staging=False, local_only=True
    ) as conn:
        draft = _read(conn, draft_id, lock=True)
        _current(draft, revision, binding, reviewed_fields)
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
        content_hash = hashlib.sha256(
            _json(
                {
                    "source": draft["source_hash"],
                    "fields": {
                        name: {"state": field.state, "value": field.value}
                        for name, field in fields.items().items()
                    },
                }
            ).encode()
        ).hexdigest()
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
                "revision": revision,
                "submission": result["submission_id"],
            },
        )
        return result
