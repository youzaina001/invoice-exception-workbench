"""Saved outputs enter through the same PDF review and explicit confirmation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from test_evaluation import make_pdf
from test_pdf_intake import HEADERS, INVOICE, action, overview, upload

from invoice_triage import pdf_intake
from invoice_triage.request_scope import install_scope, read_workspace_view


def _scoped_engine(dsn: str) -> Engine:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    install_scope(engine)
    return engine


def saved(source: bytes, raw: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": "1",
            "kind": "offline-stub",
            "arm": "synthetic-model-stub",
            "run_id": "controlled-development-example",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "result": raw,
        }
    ).encode()


def test_saved_arm_is_distinct_and_requires_the_same_confirmation(
    client: TestClient,
) -> None:
    source = make_pdf([INVOICE])
    original = upload(client, source)
    raw = deepcopy(original["raw"])
    raw["fields"]["amount_minor"]["value"] = 9999
    response = client.post(
        "/pdf-drafts",
        headers=HEADERS,
        files={
            "file": ("source.pdf", source, "application/pdf"),
            "extraction": ("saved.json", saved(source, raw), "application/json"),
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["raw"] == raw
    assert draft["extraction"]["arm"] == "synthetic-model-stub"
    assert draft["source_hash"] == original["source_hash"]
    assert draft["confirmed"] is False
    action(client, draft, "intake", status=409)
    action(client, action(client, draft, "confirm"), "intake")
    assert overview(client)["submissions"]
    assert client.post("/runs", headers=HEADERS).json()["status"] == "pass"
    assert (
        client.get(f"/pdf-drafts/{original['id']}", headers=HEADERS).json() == original
    )


@pytest.mark.parametrize(
    "fault",
    ["source", "extra", "duplicate", "span", "oversize", "non_object_null", "list"],
)
def test_invalid_saved_output_cannot_change_existing_intake(
    pg_dsns: dict[str, str], fault: str
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = make_pdf([INVOICE])
        draft = pdf_intake.create_draft(engine, filename="source.pdf", data=source)
        confirmed = pdf_intake.confirm_draft(
            engine,
            draft["id"],
            revision=draft["revision"],
            binding=draft["binding"],
        )
        pdf_intake.intake_draft(
            engine,
            confirmed["id"],
            revision=confirmed["revision"],
            binding=confirmed["binding"],
        )
        before = read_workspace_view(engine)["overview"]
        raw = json.loads(saved(source, draft["raw"]))
        if fault == "source":
            raw["source_sha256"] = "0" * 64
        elif fault == "extra":
            raw["confirmation"] = True
        elif fault == "span":
            raw["result"]["fields"]["amount_minor"]["evidence"] = [
                {"page": 99, "start": 0, "end": 4}
            ]
        data = json.dumps(raw).encode()
        if fault == "duplicate":
            data = data[:-1] + b', "version":"1"}'
        elif fault == "oversize":
            data = b" " * (1024 * 1024 + 1)
        elif fault == "non_object_null":
            data = b"null"
        elif fault == "list":
            data = b"[]"
        with pytest.raises(ValueError):
            pdf_intake.create_draft(
                engine, filename="source.pdf", data=source, extraction=data
            )
        assert read_workspace_view(engine)["overview"] == before
    finally:
        engine.dispose()


def test_edit_preserves_saved_identity_and_clears_confirmation(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = make_pdf([INVOICE])
        original = pdf_intake.create_draft(engine, filename="source.pdf", data=source)
        loaded = pdf_intake.create_draft(
            engine,
            filename="source.pdf",
            data=source,
            extraction=saved(source, original["raw"]),
        )
        draft = pdf_intake.confirm_draft(
            engine,
            loaded["id"],
            revision=loaded["revision"],
            binding=loaded["binding"],
        )
        edited = pdf_intake.edit_draft(
            engine, draft["id"], revision=1, fields=draft["fields"]
        )
        assert edited["extraction"] == draft["extraction"]
        assert edited["raw"] == draft["raw"]
        assert edited["confirmed"] is False
        assert edited["binding"] != draft["binding"]
    finally:
        engine.dispose()
