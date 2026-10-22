"""Local PDF review and explicit confirmation at the real HTTP/PG boundary."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from conftest import signed_in_reviewer
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_evaluation import make_pdf

from invoice_triage.app import create_app
from invoice_triage.settings import Settings

HEADERS = {"Accept": "application/json"}
INVOICE = (
    "Invoice ID: PDF-001\nSupplier ID: SUP-001\nInvoice date: 2026-10-23\n"
    "Currency: EUR\nInvoice total: 12.34\n"
)


def upload(client: TestClient, data: bytes | None = None) -> dict[str, Any]:
    response = client.post(
        "/pdf-drafts",
        headers=HEADERS,
        files={
            "file": ("synthetic.pdf", data or make_pdf([INVOICE]), "application/pdf")
        },
    )
    assert response.status_code == 201, response.text
    draft: dict[str, Any] = response.json()
    return draft


def token(draft: dict[str, Any]) -> dict[str, Any]:
    return {"revision": draft["revision"], "binding": draft["binding"]}


def action(
    client: TestClient, draft: dict[str, Any], name: str, *, status: int = 200
) -> dict[str, Any]:
    response = client.post(
        f"/pdf-drafts/{draft['id']}/{name}", headers=HEADERS, json=token(draft)
    )
    assert response.status_code == status, response.text
    payload: dict[str, Any] = response.json()
    return payload


def overview(client: TestClient) -> dict[str, Any]:
    response = client.get("/", headers=HEADERS)
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


def test_valid_pdf_needs_confirmation_then_runs_real_check_without_case(
    client: TestClient,
) -> None:
    source = make_pdf([INVOICE])
    draft = upload(client, source)
    assert draft["source_hash"] == hashlib.sha256(source).hexdigest()
    assert draft["filename"] == "synthetic.pdf"
    assert draft["pages"] == [INVOICE]
    assert draft["raw"]["status"] == "ok"
    assert draft["fields"]["amount_minor"]["value"] == 1234
    assert draft["confirmed"] is False
    assert draft["submitted_id"] is None
    assert client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json() == draft
    downloaded = client.get(f"/pdf-drafts/{draft['id']}/source")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/pdf")
    assert downloaded.content == source
    assert overview(client)["submissions"] == []
    action(client, draft, "intake", status=409)
    confirmed = action(client, draft, "confirm")
    assert confirmed["confirmed"] is True
    submission = action(client, confirmed, "intake")
    assert submission["invoice_ref"] == "PDF-001"
    saved = client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json()
    assert saved["submitted_id"] == submission["submission_id"]
    response = client.post("/runs", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pass"
    assert response.json()["case_id"] is None
    assert overview(client)["cases"] == []


def test_zero_amount_is_exact_integer_and_can_be_confirmed(client: TestClient) -> None:
    draft = upload(client, make_pdf([INVOICE.replace("12.34", "0.00")]))
    amount = draft["fields"]["amount_minor"]["value"]
    assert type(amount) is int
    assert amount == 0
    confirmed = action(client, draft, "confirm")
    action(client, confirmed, "intake")
    assert client.post("/runs", headers=HEADERS).json()["status"] == "pass"


def test_edit_invalidates_confirmation_preserves_raw_and_creates_only_reviewed_ref(
    client: TestClient,
) -> None:
    source = make_pdf([INVOICE])
    draft = upload(client, source)
    confirmed = action(client, draft, "confirm")
    fields = deepcopy(draft["fields"])
    fields["invoice_id"]["value"] = "PDF-REVIEWED"
    fields["amount_minor"]["value"] = 999
    response = client.post(
        f"/pdf-drafts/{draft['id']}/edit",
        headers=HEADERS,
        json={"revision": draft["revision"], "fields": fields},
    )
    assert response.status_code == 200, response.text
    edited = response.json()
    assert edited["revision"] > draft["revision"]
    assert edited["binding"] != draft["binding"]
    assert edited["confirmed"] is False
    assert edited["raw"] == draft["raw"]
    assert edited["fields"] == fields
    assert client.get(f"/pdf-drafts/{draft['id']}/source").content == source
    action(client, confirmed, "intake", status=409)
    action(client, edited, "intake", status=409)
    assert overview(client)["submissions"] == []
    corrected = action(client, edited, "confirm")
    action(client, corrected, "intake")
    rows = overview(client)["submissions"]
    assert len(rows) == 1
    assert rows[0]["invoice_ref"] == "PDF-REVIEWED"


def test_source_replacement_invalidates_confirmation_and_stale_revision(
    client: TestClient, pg_dsns: dict[str, str]
) -> None:
    original = upload(client)
    confirmed = action(client, original, "confirm")
    engine = make_engine(pg_dsns["admin"])
    with engine.connect() as conn:
        sources = (
            conn.execute(text("SELECT to_jsonb(s) FROM wb.pdf_sources s"))
            .scalars()
            .all()
        )
        revisions = (
            conn.execute(text("SELECT to_jsonb(r) FROM wb.pdf_revisions r"))
            .scalars()
            .all()
        )
    replacement = make_pdf([INVOICE.replace("12.34", "56.78")])
    response = client.post(
        f"/pdf-drafts/{original['id']}/source",
        headers=HEADERS,
        data={"revision": str(original["revision"])},
        files={"file": ("replacement.pdf", replacement, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["revision"] > original["revision"]
    assert updated["source_hash"] == hashlib.sha256(replacement).hexdigest()
    assert updated["confirmed"] is False
    assert updated["raw"]["fields"]["amount_minor"]["value"] == 5678
    assert client.get(f"/pdf-drafts/{original['id']}/source").content == replacement
    action(client, confirmed, "intake", status=409)
    action(client, updated, "intake", status=409)
    action(client, original, "confirm", status=409)
    assert overview(client)["submissions"] == []
    try:
        with engine.connect() as conn:
            saved_sources = (
                conn.execute(text("SELECT to_jsonb(s) FROM wb.pdf_sources s"))
                .scalars()
                .all()
            )
            saved_revisions = (
                conn.execute(text("SELECT to_jsonb(r) FROM wb.pdf_revisions r"))
                .scalars()
                .all()
            )
        assert all(row in saved_sources for row in sources)
        assert all(row in saved_revisions for row in revisions)
        assert len(saved_sources) == len(sources) + 1
        assert len(saved_revisions) == len(revisions) + 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("name", ["confirm", "intake"])
@pytest.mark.parametrize("bad", [{"binding": "not-the-source"}, {"revision": -1}])
def test_wrong_binding_and_revision_cannot_confirm_or_intake(
    client: TestClient, name: str, bad: dict[str, Any]
) -> None:
    draft = action(client, upload(client), "confirm")
    response = client.post(
        f"/pdf-drafts/{draft['id']}/{name}",
        headers=HEADERS,
        json={**token(draft), **bad},
    )
    assert response.status_code == 409, response.text
    assert overview(client)["submissions"] == []


@pytest.mark.parametrize(
    "source,expected",
    [
        (b"not a PDF", "failure"),
        (make_pdf([INVOICE.replace("Invoice total: 12.34\n", "")]), "ok"),
        (
            make_pdf([INVOICE + "Invoice total: 99.00\n"]),
            "ok",
        ),
    ],
    ids=["corrupt", "missing-amount", "ambiguous-amount"],
)
def test_refused_extraction_retains_source_but_never_stages(
    client: TestClient, source: bytes, expected: str
) -> None:
    draft = upload(client, source)
    assert draft["raw"]["status"] == expected
    assert client.get(f"/pdf-drafts/{draft['id']}/source").content == source
    action(client, draft, "confirm", status=400)
    action(client, draft, "intake", status=409)
    assert overview(client)["submissions"] == []
    assert client.post("/runs", headers=HEADERS).status_code == 409


@pytest.mark.parametrize("amount", [True, 12.34, -1, 9_223_372_036_854_775_808])
def test_invalid_amount_edit_is_rejected_without_changing_draft(
    client: TestClient, amount: Any
) -> None:
    draft = upload(client)
    fields = deepcopy(draft["fields"])
    fields["amount_minor"]["value"] = amount
    response = client.post(
        f"/pdf-drafts/{draft['id']}/edit",
        headers=HEADERS,
        json={"revision": draft["revision"], "fields": fields},
    )
    assert response.status_code == 400, response.text
    assert client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json() == draft
    assert overview(client)["submissions"] == []


def test_source_text_is_escaped_in_review_html(client: TestClient) -> None:
    injected = '<script>alert("source")</script>'
    draft = upload(client, make_pdf([INVOICE + injected + "\n"]))
    response = client.get(f"/pdf-drafts/{draft['id']}")
    assert response.status_code == 200
    assert injected not in response.text
    assert "&lt;script&gt;" in response.text


def test_unchanged_values_preserve_duplicate_history_after_review(
    client: TestClient,
) -> None:
    draft = action(client, upload(client), "confirm")
    original = action(client, draft, "intake")
    fields = deepcopy(draft["fields"])
    for field in fields.values():
        field["evidence"] = []
    edited = client.post(
        f"/pdf-drafts/{draft['id']}/edit",
        headers=HEADERS,
        json={"revision": draft["revision"], "fields": fields},
    ).json()
    confirmed = action(client, edited, "confirm")
    repeated = action(client, confirmed, "intake")
    assert repeated["duplicate"] is True
    assert repeated["submission_id"] == original["submission_id"]
    assert len(overview(client)["submissions"]) == 1


def test_hosted_pdf_routes_refuse_before_workspace_or_pdf_write(
    pg_dsns: dict[str, str],
) -> None:
    settings = Settings(
        hosted=True,
        intake_dsn=pg_dsns["intake"],
        read_dsn=pg_dsns["read"],
        audit_dsn=pg_dsns["audit"],
    )
    with TestClient(create_app(settings, authenticate=signed_in_reviewer)) as client:
        for reviewer in (None, "signed-in-reviewer"):
            headers = (
                HEADERS if reviewer is None else {**HEADERS, "X-Reviewer": reviewer}
            )
            requests: list[tuple[str, str, dict[str, Any]]] = [
                ("POST", "/pdf-drafts", {"files": {"file": ("a.pdf", b"bad")}}),
                ("GET", "/pdf-drafts/unknown", {}),
                ("GET", "/pdf-drafts/unknown/source", {}),
                ("POST", "/pdf-drafts/unknown/edit", {"json": {}}),
                ("POST", "/pdf-drafts/unknown/confirm", {"json": {}}),
                ("POST", "/pdf-drafts/unknown/intake", {"json": {}}),
                (
                    "POST",
                    "/pdf-drafts/unknown/source",
                    {"files": {"file": ("b.pdf", b"bad")}},
                ),
            ]
            statuses = [
                client.request(method, url, headers=headers, **kwargs).status_code
                for method, url, kwargs in requests
            ]
            assert statuses == [403] * len(requests)
    engine = make_engine(pg_dsns["admin"])
    try:
        with engine.connect() as conn:
            tables = conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'wb' "
                    "AND (tablename LIKE 'pdf_%' OR tablename IN "
                    "('demo_workspaces', 'demo_attempts', 'submissions', "
                    "'staging_invoice'))"
                )
            ).scalars()
            for table in tables:
                assert (
                    conn.execute(
                        text(f'SELECT count(*) FROM wb."{table}"')
                    ).scalar_one()
                    == 0
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize("target", ["data", "fields"])
def test_changed_stored_source_or_fields_cannot_reuse_confirmation(
    client: TestClient, pg_dsns: dict[str, str], target: str
) -> None:
    draft = action(client, upload(client), "confirm")
    engine = make_engine(pg_dsns["intake"])
    try:
        with engine.begin() as conn:
            if target == "data":
                conn.execute(
                    text("UPDATE wb.pdf_sources SET data = :data WHERE id = :id"),
                    {
                        "data": make_pdf([INVOICE.replace("12.34", "55.55")]),
                        "id": draft["source_id"],
                    },
                )
            else:
                conn.execute(
                    text(
                        "UPDATE wb.pdf_revisions SET fields = "
                        "jsonb_set(fields, '{amount_minor,value}', '5555'::jsonb) "
                        "WHERE draft_id = :id AND revision = :revision"
                    ),
                    {"id": draft["id"], "revision": draft["revision"]},
                )
        action(client, draft, "intake", status=409)
        assert overview(client)["submissions"] == []
    finally:
        engine.dispose()


def test_pdf_provenance_failure_rolls_back_submission_and_staging(
    client: TestClient, pg_dsns: dict[str, str]
) -> None:
    draft = action(client, upload(client), "confirm")
    engine = make_engine(pg_dsns["admin"])
    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                CREATE FUNCTION wb.refuse_test_pdf_intake() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'test provenance failure';
                END $$;
                CREATE TRIGGER refuse_test_pdf BEFORE INSERT ON wb.pdf_intakes
                FOR EACH ROW EXECUTE FUNCTION wb.refuse_test_pdf_intake();
            """)
            )
        with pytest.raises(DBAPIError, match="test provenance failure"):
            action(client, draft, "intake")
        assert overview(client)["submissions"] == []
        assert client.post("/runs", headers=HEADERS).status_code == 409
        saved = client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json()
        assert saved["confirmed"] is True and saved["submitted_id"] is None
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DROP TRIGGER IF EXISTS refuse_test_pdf ON wb.pdf_intakes")
            )
            conn.execute(text("DROP FUNCTION IF EXISTS wb.refuse_test_pdf_intake()"))
        engine.dispose()
    action(client, draft, "intake")
    assert len(overview(client)["submissions"]) == 1
