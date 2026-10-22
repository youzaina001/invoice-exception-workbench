"""Saved outputs enter through the same PDF review and explicit confirmation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_evaluation import make_pdf
from test_pdf_intake import HEADERS, INVOICE, action, overview, upload


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


@pytest.mark.parametrize("fault", ["source", "extra", "duplicate", "span", "oversize"])
def test_invalid_saved_output_cannot_change_existing_intake(
    client: TestClient, fault: str
) -> None:
    source = make_pdf([INVOICE])
    draft = upload(client, source)
    action(client, action(client, draft, "confirm"), "intake")
    before = overview(client)
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
    response = client.post(
        "/pdf-drafts",
        headers=HEADERS,
        files={
            "file": ("source.pdf", source, "application/pdf"),
            "extraction": ("saved.json", data, "application/json"),
        },
    )
    assert response.status_code == 400
    assert overview(client) == before


def test_edit_preserves_saved_identity_and_clears_confirmation(
    client: TestClient,
) -> None:
    source = make_pdf([INVOICE])
    original = upload(client, source)
    response = client.post(
        "/pdf-drafts",
        headers=HEADERS,
        files={
            "file": ("source.pdf", source, "application/pdf"),
            "extraction": (
                "saved.json",
                saved(source, original["raw"]),
                "application/json",
            ),
        },
    )
    draft = action(client, response.json(), "confirm")
    edited = client.post(
        f"/pdf-drafts/{draft['id']}/edit",
        headers=HEADERS,
        json={"revision": 1, "fields": draft["fields"]},
    ).json()
    assert edited["extraction"] == draft["extraction"]
    assert edited["raw"] == draft["raw"]
    assert edited["confirmed"] is False
    assert edited["binding"] != draft["binding"]
