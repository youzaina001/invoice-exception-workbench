"""Local PDF review and explicit confirmation at the PG and HTTP boundaries."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

import pytest
from airflow_dq_agent.warehouse.db import make_engine
from conftest import signed_in_reviewer
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from test_evaluation import make_pdf

from invoice_triage import pdf_intake
from invoice_triage.app import create_app
from invoice_triage.extraction import validate_fields
from invoice_triage.request_scope import install_scope, read_workspace_view
from invoice_triage.settings import Settings

HEADERS = {"Accept": "application/json"}
INVOICE = (
    "Invoice ID: PDF-001\nSupplier ID: SUP-001\nInvoice date: 2026-10-23\n"
    "Currency: EUR\nInvoice total: 12.34\n"
)


def _scoped_engine(dsn: str) -> Engine:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    install_scope(engine)
    return engine


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


def test_validate_fields_normalizes_and_checks_spans() -> None:
    raw: dict[str, Any] = {
        "invoice_id": {"state": "value", "value": "  PDF-001 ", "evidence": []},
        "supplier_id": {"state": "value", "value": " SUP-001 ", "evidence": []},
        "invoice_date": {"state": "value", "value": "2026-10-23", "evidence": []},
        "currency": {"state": "value", "value": " eur ", "evidence": []},
        "amount_minor": {
            "state": "value",
            "value": 0,
            "evidence": [{"page": 1, "start": 0, "end": 7}],
        },
    }
    normalized = validate_fields(raw, pages=[INVOICE])
    assert normalized.invoice_id.value == "PDF-001"
    assert normalized.supplier_id.value == "SUP-001"
    assert normalized.currency.value == "EUR"
    assert normalized.amount_minor.value == 0

    for bad_amount in (True, 12.34, -1, 9_223_372_036_854_775_808):
        bad = deepcopy(raw)
        bad["amount_minor"]["value"] = bad_amount
        with pytest.raises(ValueError):
            validate_fields(bad, pages=[INVOICE])

    bad_week = deepcopy(raw)
    bad_week["invoice_date"]["value"] = "2026-W42"
    with pytest.raises(ValueError):
        validate_fields(bad_week, pages=[INVOICE])

    bad_missing = deepcopy(raw)
    bad_missing["amount_minor"] = {"state": "missing", "value": 1234, "evidence": []}
    with pytest.raises(ValueError):
        validate_fields(bad_missing, pages=[INVOICE])

    bad_span = deepcopy(raw)
    bad_span["amount_minor"]["evidence"] = [{"page": 2, "start": 0, "end": 1}]
    with pytest.raises(ValueError):
        validate_fields(bad_span, pages=[INVOICE])


def test_review_rows_formats_cents_slices_evidence_and_handles_refused_draft() -> None:
    draft: dict[str, Any] = {
        "pages": [INVOICE],
        "fields": {
            "invoice_id": {"state": "value", "value": "PDF-001", "evidence": []},
            "supplier_id": {"state": "value", "value": "SUP-001", "evidence": []},
            "invoice_date": {
                "state": "value",
                "value": "2026-10-23",
                "evidence": [],
            },
            "currency": {"state": "value", "value": "EUR", "evidence": []},
            "amount_minor": {"state": "value", "value": 0, "evidence": []},
        },
        "raw": {
            "status": "ok",
            "fields": {
                "invoice_id": {
                    "evidence": [{"page": 1, "start": 12, "end": 19}],
                },
                "supplier_id": {"evidence": []},
                "invoice_date": {"evidence": []},
                "currency": {"evidence": []},
                "amount_minor": {"evidence": []},
            },
        },
    }
    rows = pdf_intake.review_rows(draft)
    by_name = {row["name"]: row for row in rows}
    assert by_name["invoice_id"]["evidence"] == [{"page": 1, "text": "PDF-001"}]
    assert by_name["amount_minor"]["value"] == "0.00"
    assert by_name["amount_minor"]["label"] == "Amount (decimal, for example 12.34)"

    draft["fields"]["amount_minor"]["value"] = 1234
    rows = pdf_intake.review_rows(draft)
    assert rows[-1]["value"] == "12.34"

    draft["fields"]["amount_minor"] = {
        "state": "missing",
        "value": None,
        "evidence": [],
    }
    rows = pdf_intake.review_rows(draft)
    assert rows[-1]["value"] == ""

    assert pdf_intake.review_rows({**draft, "fields": None}) == []


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


@pytest.mark.parametrize("state", ["missing", "ambiguous"])
def test_absent_pdf_amount_opens_case_then_replacement_requires_fresh_checks(
    client: TestClient, pg_dsns: dict[str, str], state: str
) -> None:
    content = (
        INVOICE.replace("Invoice total: 12.34\n", "")
        if state == "missing"
        else INVOICE + "Invoice total: 99.00\n"
    )
    source = make_pdf([content])
    draft = upload(client, source)
    assert draft["fields"]["amount_minor"]["state"] == state
    assert draft["fields"]["amount_minor"]["value"] is None
    confirmed = action(client, draft, "confirm")
    assert overview(client)["cases"] == []
    original = action(client, confirmed, "intake")
    assert overview(client)["cases"] == []
    failed = client.post("/runs", headers=HEADERS).json()
    assert failed["status"] == "fail"
    case_url = f"/cases/{failed['case_id']}"
    detail = client.get(case_url, headers=HEADERS).json()
    assert detail["status"] == "open"
    assert detail["row"]["amount_minor"] is None
    assert detail["core"]["sample_failures"][0]["amount_minor"] is None
    refused = client.post(f"{case_url}/resolve", headers=HEADERS)
    assert refused.status_code == 200
    assert refused.json()["resolved"] is False
    assert refused.json()["reason"] == "verdict-not-pass"

    replacement_source = make_pdf([INVOICE])
    replacement = upload(client, replacement_source)
    assert replacement["id"] != draft["id"]
    action(client, replacement, "intake", status=409)
    wrong_confirmation = client.post(
        f"/pdf-drafts/{replacement['id']}/intake",
        headers=HEADERS,
        json=token(confirmed),
    )
    assert wrong_confirmation.status_code == 409
    corrected = action(client, action(client, replacement, "confirm"), "intake")
    assert corrected["invoice_ref"] == original["invoice_ref"] == "PDF-001"
    assert corrected["version"] == 2
    assert corrected["submission_id"] != original["submission_id"]
    stale = client.post(f"{case_url}/resolve", headers=HEADERS)
    assert stale.status_code == 200
    assert stale.json()["resolved"] is False
    assert stale.json()["reason"] == "stale-version"

    # Replaying the old PDF must not replace the current corrected submission.
    duplicate = action(
        client, action(client, upload(client, source), "confirm"), "intake"
    )
    assert duplicate["duplicate"] is True
    assert duplicate["submission_id"] == original["submission_id"]
    passed = client.post("/runs", headers=HEADERS).json()
    assert passed["status"] == "pass"
    assert passed["case_id"] == failed["case_id"]
    detail = client.get(case_url, headers=HEADERS).json()
    assert detail["status"] == "open"
    assert detail["resolution"] == {"allowed": True, "reason": "ok"}
    assert detail["checked_submission_id"] == corrected["submission_id"]
    assert [s["row"]["amount_minor"] for s in detail["history"]["submissions"]] == [
        None,
        1234,
    ]
    assert {event["run_id"] for event in detail["history"]["audit"]} == {
        failed["run_id"],
        passed["run_id"],
    }
    resolved = client.post(f"{case_url}/resolve", headers=HEADERS)
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    assert client.get(case_url, headers=HEADERS).json()["status"] == "resolved"
    saved = client.get(f"/pdf-drafts/{draft['id']}", headers=HEADERS).json()
    assert saved["fields"] == draft["fields"]
    assert saved["raw"] == draft["raw"]
    assert client.get(f"/pdf-drafts/{draft['id']}/source").content == source
    assert (
        client.get(f"/pdf-drafts/{replacement['id']}/source").content
        == replacement_source
    )
    engine = make_engine(pg_dsns["admin"])
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT resolved_run_id FROM wb.cases WHERE id = :id"),
                    {"id": failed["case_id"]},
                ).scalar_one()
                == passed["run_id"]
            )
    finally:
        engine.dispose()


def test_case_preserves_ambiguous_amount_state_after_a_later_draft_edit(
    client: TestClient,
) -> None:
    draft = upload(client, make_pdf([INVOICE + "Invoice total: 99.00\n"]))
    action(client, action(client, draft, "confirm"), "intake")
    failed = client.post("/runs", headers=HEADERS).json()
    fields = deepcopy(draft["fields"])
    fields["amount_minor"] = {"state": "missing", "value": None, "evidence": []}
    client.post(
        f"/pdf-drafts/{draft['id']}/edit",
        headers=HEADERS,
        json={"revision": draft["revision"], "fields": fields},
    ).raise_for_status()
    case_url = f"/cases/{failed['case_id']}"
    case = client.get(case_url, headers=HEADERS).json()
    assert case["amount_state"] == "ambiguous"
    assert case["history"]["submissions"][0]["amount_state"] == "ambiguous"
    page = client.get(case_url).text
    assert "<td>ambiguous</td>" in page
    assert "upload and confirm a corrected PDF" in page


@pytest.mark.parametrize("state", ["missing", "ambiguous"])
@pytest.mark.parametrize("action_name", ["edit", "confirm", "intake"])
@pytest.mark.parametrize("form", [False, True])
def test_absent_amount_with_a_number_refuses_without_writes(
    pg_dsns: dict[str, str], state: str, action_name: str, form: bool
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = make_pdf([INVOICE])
        initial = pdf_intake.create_draft(engine, filename="synthetic.pdf", data=source)
        draft = pdf_intake.confirm_draft(
            engine,
            initial["id"],
            revision=initial["revision"],
            binding=initial["binding"],
        )
        fields = deepcopy(draft["fields"])
        fields["amount_minor"]["state"] = state
        payload: dict[str, Any]
        if not form:
            payload = {**token(draft), "fields": fields}
        else:
            payload = {name: str(field["value"]) for name, field in fields.items()}
            payload["amount_minor"] = "12.34"
            payload.update(
                {f"{name}_state": field["state"] for name, field in fields.items()}
            )
            payload.update({name: str(value) for name, value in token(draft).items()})
        fn = getattr(pdf_intake, f"{action_name}_draft")
        with pytest.raises(ValueError):
            fn(engine, draft["id"], payload=payload, form=form)
        assert pdf_intake.read_draft(engine, draft["id"]) == draft
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()


def test_edit_invalidates_confirmation_preserves_raw_and_creates_only_reviewed_ref(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = make_pdf([INVOICE])
        draft = pdf_intake.create_draft(engine, filename="synthetic.pdf", data=source)
        confirmed = pdf_intake.confirm_draft(engine, draft["id"], payload=token(draft))
        fields = deepcopy(draft["fields"])
        fields["invoice_id"]["value"] = "PDF-REVIEWED"
        fields["amount_minor"]["value"] = 999
        edited = pdf_intake.edit_draft(
            engine,
            draft["id"],
            revision=draft["revision"],
            fields=fields,
        )
        assert edited["revision"] > draft["revision"]
        assert edited["binding"] != draft["binding"]
        assert edited["confirmed"] is False
        assert edited["raw"] == draft["raw"]
        assert edited["fields"] == fields
        assert pdf_intake.read_source(engine, draft["id"]) == source
        for stale in (confirmed, edited):
            with pytest.raises(pdf_intake.PDFConflict):
                pdf_intake.intake_draft(engine, draft["id"], payload=token(stale))
        assert read_workspace_view(engine)["overview"]["submissions"] == []
        corrected = pdf_intake.confirm_draft(
            engine, edited["id"], payload=token(edited)
        )
        pdf_intake.intake_draft(engine, corrected["id"], payload=token(corrected))
        rows = read_workspace_view(engine)["overview"]["submissions"]
        assert len(rows) == 1
        assert rows[0]["invoice_ref"] == "PDF-REVIEWED"
    finally:
        engine.dispose()


def test_source_replacement_invalidates_confirmation_and_stale_revision(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    admin_engine = make_engine(pg_dsns["admin"])
    try:
        original = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        confirmed = pdf_intake.confirm_draft(
            engine, original["id"], payload=token(original)
        )
        with admin_engine.connect() as conn:
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
        updated = pdf_intake.replace_source(
            engine,
            original["id"],
            payload={"revision": str(original["revision"])},
            filename="replacement.pdf",
            data=replacement,
        )
        assert updated["revision"] > original["revision"]
        assert updated["source_hash"] == hashlib.sha256(replacement).hexdigest()
        assert updated["confirmed"] is False
        assert updated["raw"]["fields"]["amount_minor"]["value"] == 5678
        assert pdf_intake.read_source(engine, original["id"]) == replacement
        for attempt in (confirmed, updated):
            with pytest.raises(pdf_intake.PDFConflict):
                pdf_intake.intake_draft(engine, attempt["id"], payload=token(attempt))
        with pytest.raises(pdf_intake.PDFConflict):
            pdf_intake.confirm_draft(engine, original["id"], payload=token(original))
        assert read_workspace_view(engine)["overview"]["submissions"] == []
        with admin_engine.connect() as conn:
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
        admin_engine.dispose()


@pytest.mark.parametrize("name", ["confirm", "intake"])
@pytest.mark.parametrize("bad", [{"binding": "not-the-source"}, {"revision": -1}])
def test_wrong_binding_and_revision_cannot_confirm_or_intake(
    pg_dsns: dict[str, str], name: str, bad: dict[str, Any]
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        initial = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        draft = pdf_intake.confirm_draft(engine, initial["id"], payload=token(initial))
        fn = getattr(pdf_intake, f"{name}_draft")
        with pytest.raises(pdf_intake.PDFConflict):
            fn(engine, draft["id"], payload={**token(draft), **bad})
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()


def test_refused_extraction_retains_source_but_never_stages(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = b"not a PDF"
        draft = pdf_intake.create_draft(engine, filename="bad.pdf", data=source)
        assert draft["raw"]["status"] == "failure"
        assert pdf_intake.read_source(engine, draft["id"]) == source
        with pytest.raises(ValueError):
            pdf_intake.confirm_draft(engine, draft["id"], payload=token(draft))
        with pytest.raises(pdf_intake.PDFConflict):
            pdf_intake.intake_draft(engine, draft["id"], payload=token(draft))
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("amount", [True, 12.34, -1, 9_223_372_036_854_775_808])
def test_invalid_amount_edit_is_rejected_without_changing_draft(
    pg_dsns: dict[str, str], amount: Any
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        draft = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        fields = deepcopy(draft["fields"])
        fields["amount_minor"]["value"] = amount
        with pytest.raises(ValueError):
            pdf_intake.edit_draft(
                engine, draft["id"], revision=draft["revision"], fields=fields
            )
        assert pdf_intake.read_draft(engine, draft["id"]) == draft
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("raw_amount", ["1e2", "+12.34", "1.000", "abc"])
def test_form_amount_edit_enforces_strict_decimal_dot(
    pg_dsns: dict[str, str], raw_amount: str
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        draft = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        fields = deepcopy(draft["fields"])
        payload: dict[str, Any] = {
            name: str(field["value"]) for name, field in fields.items()
        }
        payload["amount_minor"] = raw_amount
        payload.update(
            {f"{name}_state": field["state"] for name, field in fields.items()}
        )
        payload["revision"] = str(draft["revision"])
        with pytest.raises(ValueError):
            pdf_intake.edit_draft(engine, draft["id"], payload=payload, form=True)
        assert pdf_intake.read_draft(engine, draft["id"]) == draft
    finally:
        engine.dispose()


def test_source_text_is_escaped_in_review_html(client: TestClient) -> None:
    injected = '<script>alert("source")</script>'
    draft = upload(client, make_pdf([INVOICE + injected + "\n"]))
    response = client.get(f"/pdf-drafts/{draft['id']}")
    assert response.status_code == 200
    assert injected not in response.text
    assert "&lt;script&gt;" in response.text


def test_unchanged_values_preserve_duplicate_history_after_review(
    pg_dsns: dict[str, str],
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        initial = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        draft = pdf_intake.confirm_draft(engine, initial["id"], payload=token(initial))
        original = pdf_intake.intake_draft(engine, draft["id"], payload=token(draft))
        fields = deepcopy(draft["fields"])
        for field in fields.values():
            field["evidence"] = []
        edited = pdf_intake.edit_draft(
            engine, draft["id"], revision=draft["revision"], fields=fields
        )
        confirmed = pdf_intake.confirm_draft(
            engine, edited["id"], payload=token(edited)
        )
        repeated = pdf_intake.intake_draft(
            engine, confirmed["id"], payload=token(confirmed)
        )
        assert repeated["duplicate"] is True
        assert repeated["submission_id"] == original["submission_id"]
        assert len(read_workspace_view(engine)["overview"]["submissions"]) == 1
    finally:
        engine.dispose()


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
@pytest.mark.parametrize("path", ["new", "duplicate", "restage"])
def test_changed_stored_source_or_fields_cannot_reuse_confirmation(
    pg_dsns: dict[str, str], target: str, path: str
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        initial = pdf_intake.create_draft(
            engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        draft = pdf_intake.confirm_draft(engine, initial["id"], payload=token(initial))
        if path != "new":
            pdf_intake.intake_draft(engine, draft["id"], payload=token(draft))
        if path == "restage":
            other = pdf_intake.create_draft(
                engine,
                filename="other.pdf",
                data=make_pdf([INVOICE.replace("PDF-001", "PDF-OTHER")]),
            )
            confirmed_other = pdf_intake.confirm_draft(
                engine, other["id"], payload=token(other)
            )
            pdf_intake.intake_draft(
                engine, confirmed_other["id"], payload=token(confirmed_other)
            )
        before = read_workspace_view(engine)["overview"]
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
        with pytest.raises(pdf_intake.PDFConflict):
            pdf_intake.intake_draft(engine, draft["id"], payload=token(draft))
        assert read_workspace_view(engine)["overview"] == before
    finally:
        engine.dispose()


def test_pdf_provenance_failure_rolls_back_submission_and_staging(
    pg_dsns: dict[str, str],
) -> None:
    intake_engine = _scoped_engine(pg_dsns["intake"])
    admin_engine = make_engine(pg_dsns["admin"])
    try:
        initial = pdf_intake.create_draft(
            intake_engine, filename="synthetic.pdf", data=make_pdf([INVOICE])
        )
        draft = pdf_intake.confirm_draft(
            intake_engine, initial["id"], payload=token(initial)
        )
        try:
            with admin_engine.begin() as conn:
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
                pdf_intake.intake_draft(
                    intake_engine, draft["id"], payload=token(draft)
                )
            assert read_workspace_view(intake_engine)["overview"]["submissions"] == []
            saved = pdf_intake.read_draft(intake_engine, draft["id"])
            assert saved["confirmed"] is True and saved["submitted_id"] is None
        finally:
            with admin_engine.begin() as conn:
                conn.execute(
                    text("DROP TRIGGER IF EXISTS refuse_test_pdf ON wb.pdf_intakes")
                )
                conn.execute(
                    text("DROP FUNCTION IF EXISTS wb.refuse_test_pdf_intake()")
                )
        pdf_intake.intake_draft(intake_engine, draft["id"], payload=token(draft))
        assert len(read_workspace_view(intake_engine)["overview"]["submissions"]) == 1
    finally:
        intake_engine.dispose()
        admin_engine.dispose()


@pytest.mark.parametrize("name", ["confirm", "intake"])
def test_visible_fields_cannot_make_a_refused_source_reviewable(
    pg_dsns: dict[str, str], name: str
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        valid = pdf_intake.create_draft(
            engine, filename="valid.pdf", data=make_pdf([INVOICE])
        )
        fields = deepcopy(valid["fields"])
        for field in fields.values():
            field["evidence"] = []
        refused = pdf_intake.create_draft(engine, filename="bad.pdf", data=b"not a PDF")
        fn = getattr(pdf_intake, f"{name}_draft")
        with pytest.raises(ValueError):
            fn(engine, refused["id"], payload={**token(refused), "fields": fields})
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "label", ["Invoice ID", "Invoice-ID", "Invoice number", "Invoice no.", "Invoice #"]
)
@pytest.mark.parametrize("separator", [": ", " ", "\t"])
def test_multiple_invoice_ids_are_refused_before_review_and_intake(
    pg_dsns: dict[str, str], label: str, separator: str
) -> None:
    engine = _scoped_engine(pg_dsns["intake"])
    try:
        source = make_pdf(
            [
                INVOICE.replace("Invoice ID: PDF-001", f"{label}{separator}{ref}")
                for ref in ("PDF-A", "PDF-B")
            ]
        )
        draft = pdf_intake.create_draft(engine, filename="multi.pdf", data=source)
        assert draft["raw"]["status"] == "failure"
        assert draft["raw"]["code"] == "multiple_invoices"
        assert draft["fields"] is None
        with pytest.raises(ValueError):
            pdf_intake.edit_draft(
                engine,
                draft["id"],
                payload={"revision": draft["revision"], "fields": {}},
            )
        with pytest.raises(ValueError):
            pdf_intake.confirm_draft(engine, draft["id"], payload=token(draft))
        with pytest.raises(pdf_intake.PDFConflict):
            pdf_intake.intake_draft(engine, draft["id"], payload=token(draft))
        assert pdf_intake.read_source(engine, draft["id"]) == source
        assert read_workspace_view(engine)["overview"]["submissions"] == []
    finally:
        engine.dispose()
