"""Invoice Triage FastAPI application: JSON actions plus server-rendered pages."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from starlette.datastructures import UploadFile as FormUploadFile

from invoice_triage import pdf_intake
from invoice_triage.auth import verified_reviewer
from invoice_triage.extraction import Fields, load_json
from invoice_triage.governance import CheckMissingError, configure_governance
from invoice_triage.request_scope import (
    AttemptNotFoundError,
    AttemptReadOnlyError,
    ExecutionScope,
    install_scope,
    read_workspace_view,
    resolve_scope,
)
from invoice_triage.resolution import refusal_guidance
from invoice_triage.services import (
    CaseNotFoundError,
    NothingStagedError,
    StaleStagedError,
    get_case_detail,
    ingest_submission,
    parse_amount_minor,
    resolve_case,
    run_current_checks,
)
from invoice_triage.settings import Settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# A single-invoice CSV is tiny; cap the body so one request cannot buffer
# an unbounded upload into the web process.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    data: dict[str, Any],
    status_code: int = 200,
) -> Response:
    if wants_json(request):
        return JSONResponse(data, status_code=status_code)
    return TEMPLATES.TemplateResponse(request, template, context, status_code)


def _error(request: Request, detail: str, status_code: int) -> Response:
    return _render(
        request, "error.html", {"error": detail}, {"detail": detail}, status_code
    )


def _index_error(
    request: Request,
    engine: Engine,
    detail: str,
    status_code: int,
    *,
    scope: ExecutionScope,
) -> Response:
    view = read_workspace_view(engine, scope=scope)
    return _render(
        request,
        "index.html",
        {
            "overview": view["overview"],
            "attempts": view["attempts"],
            "error": detail,
            "last_run": None,
            "last_status": None,
        },
        {"detail": detail},
        status_code,
    )


def _engines(app: FastAPI) -> tuple[Engine, str, Engine]:
    settings: Settings = app.state.settings
    if not settings.intake_dsn or not settings.read_dsn or not settings.audit_dsn:
        raise RuntimeError("intake, read, and audit DSNs must all be configured")
    return app.state.intake_engine, settings.read_dsn, app.state.audit_engine


def _pdf_render(request: Request, draft: dict[str, Any], status: int = 200) -> Response:
    if request.method == "POST" and not wants_json(request):
        return RedirectResponse(f"/pdf-drafts/{draft['id']}", status_code=303)
    rows = []
    labels = {
        "invoice_id": "Invoice ID",
        "supplier_id": "Supplier ID",
        "invoice_date": "Invoice date",
        "currency": "Currency",
        "amount_minor": "Amount (decimal, for example 12.34)",
    }
    for name, field in (draft["fields"] or {}).items():
        value = field["value"]
        if name == "amount_minor" and value is not None:
            value = f"{value // 100}.{value % 100:02d}"
        rows.append(
            {
                "name": name,
                "label": labels[name],
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
    return _render(
        request, "pdf_review.html", {"draft": draft, "rows": rows}, draft, status
    )


def _pdf_error(request: Request, exc: ValueError | LookupError) -> Response:
    status = (
        409
        if isinstance(exc, pdf_intake.PDFConflict)
        else 404
        if isinstance(exc, LookupError)
        else 400
    )
    return _error(request, str(exc), status)


async def _pdf_payload(request: Request) -> dict[str, Any]:
    if "application/json" in request.headers.get("content-type", ""):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("Expected a review object")
        return cast(dict[str, Any], value)
    return dict(await request.form())


def _pdf_revision(payload: dict[str, Any]) -> int:
    value = payload.get("revision")
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if type(value) is not int:
        raise ValueError("A reviewed revision is required")
    return value


def _pdf_binding(payload: dict[str, Any]) -> str:
    binding = payload.get("binding")
    if not isinstance(binding, str) or len(binding) > 64:
        raise ValueError("A source and revision binding is required")
    return binding


def _pdf_form_fields(payload: dict[str, Any]) -> dict[str, Any]:
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


def _reviewer_id(request: Request) -> str | None:
    return cast(str | None, getattr(request.state, "reviewer", None))


def _unsigned_response(request: Request) -> Response | None:
    """Hosted routes stay closed until a Reviewer is signed in.

    The body is a refusal only: no submissions, cases, check results, or
    audit evidence, and no public registration offer.
    """
    settings: Settings = request.app.state.settings
    if not settings.hosted or _reviewer_id(request) is not None:
        return None
    return _render(request, "sign_in.html", {}, {"detail": "sign-in required"}, 401)


def _named_attempt(request: Request) -> str | None:
    raw = request.headers.get("X-Demo-Attempt")
    if raw is None:
        return None
    named = raw.strip()
    return named or None


def _request_scope(
    request: Request, *, start_fresh: bool = False
) -> ExecutionScope | Response:
    denied = _unsigned_response(request)
    if denied is not None:
        return denied
    settings: Settings = request.app.state.settings
    if not settings.hosted:
        return ExecutionScope.local()
    reviewer = _reviewer_id(request)
    assert reviewer is not None
    intake, _, _ = _engines(request.app)
    return resolve_scope(
        intake,
        hosted=True,
        reviewer_id=reviewer,
        requested_attempt_id=_named_attempt(request),
        start_fresh=start_fresh,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        for engine in (app.state.intake_engine, app.state.audit_engine):
            if engine is not None:
                engine.dispose()


def create_app(
    settings: Settings | None = None,
    *,
    authenticate: Callable[[Request], str | None] = verified_reviewer,
) -> FastAPI:
    configure_governance(scope_installer=install_scope)
    resolved = settings or Settings()
    app = FastAPI(title="Invoice Triage", lifespan=lifespan)
    app.state.settings = resolved
    app.state.intake_engine = (
        create_engine(resolved.intake_dsn, future=True, pool_pre_ping=True)
        if resolved.intake_dsn
        else None
    )
    app.state.audit_engine = (
        create_engine(resolved.audit_dsn, future=True, pool_pre_ping=True)
        if resolved.audit_dsn
        else None
    )
    for engine in (app.state.intake_engine, app.state.audit_engine):
        if engine is not None:
            install_scope(engine)

    @app.exception_handler(AttemptReadOnlyError)
    async def read_only_error(request: Request, exc: AttemptReadOnlyError) -> Response:
        if wants_json(request):
            return JSONResponse(
                {"detail": str(exc), "reason": str(exc)}, status_code=409
            )
        return _error(request, str(exc), 409)

    @app.middleware("http")
    async def _hosted_scope(request: Request, call_next: Any) -> Response:
        settings: Settings = request.app.state.settings
        if settings.hosted and (
            request.url.path == "/pdf-drafts"
            or request.url.path.startswith("/pdf-drafts/")
        ):
            return _error(request, "PDF review is available locally only", 403)
        request.state.reviewer = authenticate(request) if settings.hosted else None
        return cast(Response, await call_next(request))

    @app.get("/")
    def index(request: Request) -> Response:
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        intake, _, _ = _engines(request.app)
        view = read_workspace_view(intake, scope=scope)
        context = {
            "overview": view["overview"],
            "attempts": view["attempts"],
            "last_run": request.query_params.get("last_run"),
            "last_status": request.query_params.get("last_status"),
            "duplicate": request.query_params.get("duplicate"),
            "duplicate_ref": request.query_params.get("invoice_ref"),
            "duplicate_version": request.query_params.get("version"),
            "made_current": request.query_params.get("made_current"),
        }
        return _render(request, "index.html", context, view["overview"])

    @app.post("/demo-attempts/start-fresh")
    def start_fresh(request: Request) -> Response:
        denied = _unsigned_response(request)
        if denied is not None:
            return denied
        if not request.app.state.settings.hosted:
            return _error(request, "not found", 404)
        scope = _request_scope(request, start_fresh=True)
        if isinstance(scope, Response):
            return scope
        started = {
            "workspace_id": scope.workspace_id,
            "attempt_id": scope.attempt_id,
            "previous_attempt_id": scope.previous_attempt_id,
        }
        if wants_json(request):
            return JSONResponse(started)
        return RedirectResponse("/", status_code=303)

    @app.get("/demo-attempts/{attempt_id}")
    def read_attempt(request: Request, attempt_id: str) -> Response:
        denied = _unsigned_response(request)
        if denied is not None:
            return denied
        if not request.app.state.settings.hosted:
            return _error(request, "attempt not found", 404)
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        intake, _, audit = _engines(request.app)
        try:
            payload = read_workspace_view(
                intake, audit=audit, scope=scope, attempt_id=attempt_id
            )
        except AttemptNotFoundError as exc:
            return _error(request, str(exc), 404)
        if wants_json(request):
            return JSONResponse(payload)
        return _render(
            request,
            "index.html",
            {"overview": payload, "read_only": True},
            payload,
        )

    @app.post("/pdf-drafts")
    def upload_pdf(
        request: Request, file: UploadFile, extraction: UploadFile | None = None
    ) -> Response:
        intake, _, _ = _engines(request.app)
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return _error(request, "PDF exceeds the 5 MiB limit", 413)
        try:
            saved = None
            if extraction is not None:
                saved_data = extraction.file.read(1024 * 1024 + 1)
                if len(saved_data) > 1024 * 1024:
                    raise ValueError("Saved extraction exceeds 1 MiB")
                saved = load_json(saved_data)
                if saved is None:
                    raise ValueError("Saved extraction must be an object")
            draft = pdf_intake.create_draft(
                intake,
                filename=file.filename or "upload.pdf",
                data=data,
                extraction=saved,
            )
        except ValueError as exc:
            return _pdf_error(request, exc)
        if wants_json(request):
            return _pdf_render(request, draft, 201)
        return RedirectResponse(f"/pdf-drafts/{draft['id']}", status_code=303)

    @app.get("/pdf-drafts/{draft_id}")
    def view_pdf(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            return _pdf_render(request, pdf_intake.read_draft(intake, draft_id))
        except LookupError as exc:
            return _pdf_error(request, exc)

    @app.get("/pdf-drafts/{draft_id}/source")
    def pdf_source(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            data = pdf_intake.read_source(intake, draft_id)
        except LookupError as exc:
            return _pdf_error(request, exc)
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'attachment; filename="source.pdf"',
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
            },
        )

    @app.post("/pdf-drafts/{draft_id}/edit")
    async def edit_pdf(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            payload = await _pdf_payload(request)
            fields = (
                payload["fields"] if "fields" in payload else _pdf_form_fields(payload)
            )
            draft = pdf_intake.edit_draft(
                intake, draft_id, revision=_pdf_revision(payload), fields=fields
            )
        except (ValueError, LookupError) as exc:
            return _pdf_error(request, exc)
        return _pdf_render(request, draft)

    @app.post("/pdf-drafts/{draft_id}/source")
    async def replace_pdf_source(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            payload = await _pdf_payload(request)
            file = payload.get("file")
            if not isinstance(file, FormUploadFile):
                raise ValueError("A replacement PDF is required")
            data = file.file.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                return _error(request, "PDF exceeds the 5 MiB limit", 413)
            draft = pdf_intake.replace_source(
                intake,
                draft_id,
                revision=_pdf_revision(payload),
                filename=file.filename or "upload.pdf",
                data=data,
            )
        except (ValueError, LookupError) as exc:
            return _pdf_error(request, exc)
        return _pdf_render(request, draft)

    @app.post("/pdf-drafts/{draft_id}/confirm")
    async def confirm_pdf(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            payload = await _pdf_payload(request)
            draft = pdf_intake.confirm_draft(
                intake,
                draft_id,
                revision=_pdf_revision(payload),
                binding=_pdf_binding(payload),
                reviewed_fields=(
                    payload.get("fields")
                    if "application/json" in request.headers.get("content-type", "")
                    else _pdf_form_fields(payload)
                ),
            )
        except (ValueError, LookupError) as exc:
            return _pdf_error(request, exc)
        return _pdf_render(request, draft)

    @app.post("/pdf-drafts/{draft_id}/intake")
    async def intake_pdf(request: Request, draft_id: str) -> Response:
        intake, _, _ = _engines(request.app)
        try:
            payload = await _pdf_payload(request)
            submission = pdf_intake.intake_draft(
                intake,
                draft_id,
                revision=_pdf_revision(payload),
                binding=_pdf_binding(payload),
                reviewed_fields=(
                    payload.get("fields")
                    if "application/json" in request.headers.get("content-type", "")
                    else _pdf_form_fields(payload)
                ),
            )
        except (ValueError, LookupError) as exc:
            return _pdf_error(request, exc)
        if wants_json(request):
            return JSONResponse(submission)
        return RedirectResponse("/", status_code=303)

    @app.post("/ingest")
    def ingest(request: Request, file: UploadFile) -> Response:
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        if scope.is_read_only_attempt:
            raise AttemptReadOnlyError("attempt-read-only")
        intake, _, _ = _engines(request.app)
        filename = file.filename or "upload.csv"
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return _index_error(
                request,
                intake,
                f"{filename}: upload exceeds {MAX_UPLOAD_BYTES} bytes",
                status_code=413,
                scope=scope,
            )
        try:
            submission = ingest_submission(
                intake, filename=filename, data=data, scope=scope
            )
        except ValueError as exc:
            return _index_error(request, intake, str(exc), status_code=400, scope=scope)
        if wants_json(request):
            return JSONResponse(submission)
        if submission["duplicate"]:
            query = urlencode(
                {
                    "duplicate": "true",
                    "invoice_ref": str(submission["invoice_ref"]),
                    "version": str(submission["version"]),
                    "made_current": str(bool(submission["made_current"])).lower(),
                }
            )
            return RedirectResponse(f"/?{query}", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/runs")
    def run_checks(request: Request) -> Response:
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        intake, read_dsn, _ = _engines(request.app)
        try:
            run = run_current_checks(intake=intake, read_dsn=read_dsn, scope=scope)
        except (NothingStagedError, StaleStagedError, CheckMissingError) as exc:
            return _error(
                request, str(exc), 502 if isinstance(exc, CheckMissingError) else 409
            )
        if wants_json(request):
            return JSONResponse(run)
        if run["case_id"] is None:
            return RedirectResponse(
                f"/?last_status={run['status']}&last_run={run['run_id']}",
                status_code=303,
            )
        return RedirectResponse(f"/cases/{run['case_id']}", status_code=303)

    @app.get("/cases/{case_id}")
    def get_case(request: Request, case_id: str) -> Response:
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        intake, _, audit = _engines(request.app)
        try:
            case = get_case_detail(
                intake=intake, audit=audit, case_id=case_id, scope=scope
            )
        except CaseNotFoundError as exc:
            return _error(request, str(exc), 404)
        refused = request.query_params.get("resolve_refused")
        context: dict[str, Any] = {
            "case": case,
            "next_action": refusal_guidance(
                cast(dict[str, Any], case["resolution"])["reason"]
            ),
        }
        if refused:
            context["resolve_refused"] = refused
            context["refusal_guidance"] = refusal_guidance(refused)
        return _render(request, "case.html", context, case)

    @app.post("/cases/{case_id}/resolve")
    def resolve(request: Request, case_id: str) -> Response:
        scope = _request_scope(request)
        if isinstance(scope, Response):
            return scope
        intake, _, audit = _engines(request.app)
        try:
            outcome = resolve_case(
                intake=intake, audit=audit, case_id=case_id, scope=scope
            )
        except CaseNotFoundError as exc:
            return _error(request, str(exc), 404)
        if wants_json(request):
            return JSONResponse(cast(dict[str, Any], outcome))
        if outcome["resolved"]:
            return RedirectResponse(f"/cases/{case_id}", status_code=303)
        query = urlencode({"resolve_refused": str(outcome["reason"])})
        return RedirectResponse(f"/cases/{case_id}?{query}", status_code=303)

    return app


app = create_app()
