"""LedgerLens FastAPI application: JSON actions plus server-rendered pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from airflow_dq_agent.warehouse.db import make_engine
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine

from ledgerlens.resolution import refusal_guidance
from ledgerlens.services import (
    CaseNotFoundError,
    CheckMissingError,
    NothingStagedError,
    StaleStagedError,
    get_case_detail,
    ingest_submission,
    list_overview,
    resolve_case,
    run_current_checks,
)
from ledgerlens.settings import Settings

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


def _engines(app: FastAPI) -> tuple[Engine, str, str]:
    settings: Settings = app.state.settings
    if not settings.intake_dsn or not settings.read_dsn or not settings.audit_dsn:
        raise RuntimeError("intake, read, and audit DSNs must all be configured")
    engine = getattr(app.state, "intake_engine", None)
    if engine is None:
        engine = make_engine(settings.intake_dsn)
        app.state.intake_engine = engine
    return engine, settings.read_dsn, settings.audit_dsn


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings()
    app = FastAPI(title="LedgerLens")
    app.state.settings = resolved

    @app.get("/")
    def index(request: Request) -> Response:
        intake, _, _ = _engines(request.app)
        overview = list_overview(intake)
        context = {
            "overview": overview,
            "last_run": request.query_params.get("last_run"),
            "last_status": request.query_params.get("last_status"),
            "duplicate": request.query_params.get("duplicate"),
            "duplicate_ref": request.query_params.get("invoice_ref"),
            "duplicate_version": request.query_params.get("version"),
            "made_current": request.query_params.get("made_current"),
        }
        return _render(request, "index.html", context, overview)

    @app.post("/ingest")
    def ingest(request: Request, file: UploadFile) -> Response:
        intake, _, _ = _engines(request.app)
        filename = file.filename or "upload.csv"
        try:
            submission = ingest_submission(
                intake, filename=filename, data=file.file.read()
            )
        except ValueError as exc:
            overview = list_overview(intake)
            return _render(
                request,
                "index.html",
                {
                    "overview": overview,
                    "error": str(exc),
                    "last_run": None,
                    "last_status": None,
                },
                {"detail": str(exc)},
                status_code=400,
            )
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
        intake, read_dsn, _ = _engines(request.app)
        try:
            run = run_current_checks(intake=intake, read_dsn=read_dsn)
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
        intake, _, audit_dsn = _engines(request.app)
        try:
            case = get_case_detail(intake=intake, audit_dsn=audit_dsn, case_id=case_id)
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
        intake, _, audit_dsn = _engines(request.app)
        try:
            outcome = resolve_case(intake=intake, audit_dsn=audit_dsn, case_id=case_id)
        except CaseNotFoundError as exc:
            return _error(request, str(exc), 404)
        if wants_json(request):
            return JSONResponse(outcome)
        if outcome["resolved"]:
            return RedirectResponse(f"/cases/{case_id}", status_code=303)
        query = urlencode({"resolve_refused": str(outcome["reason"])})
        return RedirectResponse(f"/cases/{case_id}?{query}", status_code=303)

    return app


app = create_app()
