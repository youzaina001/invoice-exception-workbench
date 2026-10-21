"""Invoice Triage FastAPI application: JSON actions plus server-rendered pages."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from airflow_dq_agent.warehouse.db import make_engine
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine

from invoice_triage.auth import verified_reviewer
from invoice_triage.request_scope import (
    activate_connection_scope,
    bind_scope,
    current_attempt,
    install_scope,
    reset_scope,
    set_attempt,
)
from invoice_triage.resolution import refusal_guidance
from invoice_triage.services import (
    AttemptNotFoundError,
    AttemptReadOnlyError,
    CaseNotFoundError,
    CheckMissingError,
    NothingStagedError,
    StaleStagedError,
    ensure_demo_workspace,
    get_case_detail,
    ingest_submission,
    list_demo_attempts,
    list_overview,
    read_demo_attempt,
    resolve_case,
    run_current_checks,
    start_fresh_attempt,
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
    request: Request, engine: Engine, detail: str, status_code: int
) -> Response:
    attempt_id = current_attempt()
    return _render(
        request,
        "index.html",
        {
            "overview": {
                **list_overview(engine, attempt_id=attempt_id),
                "attempt_id": attempt_id,
            },
            "attempts": list_demo_attempts(engine) if attempt_id else [],
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


def _read_only_attempt(request: Request, current: str | None) -> Response | None:
    """A named non-current Demo Attempt accepts reads and refuses writes."""
    named = _named_attempt(request)
    if named is None or current is None or named == current:
        return None
    payload = {"detail": "attempt-read-only", "reason": "attempt-read-only"}
    if wants_json(request):
        return JSONResponse(payload, status_code=409)
    return _error(request, "attempt-read-only", 409)


def _prepare_demo(request: Request) -> dict[str, str] | None:
    """Open this Reviewer's Demo Workspace on the request's copied context."""
    settings: Settings = request.app.state.settings
    if not settings.hosted:
        return None
    reviewer = _reviewer_id(request)
    if reviewer is None:
        return None
    intake, _, _ = _engines(request.app)
    demo = ensure_demo_workspace(intake, reviewer)
    set_attempt(demo["attempt_id"])
    return demo


def _prepare_mutation(request: Request) -> dict[str, str] | Response | None:
    denied = _unsigned_response(request)
    if denied is not None:
        return denied
    demo = _prepare_demo(request)
    read_only = _read_only_attempt(
        request, None if demo is None else demo["attempt_id"]
    )
    return read_only if read_only is not None else demo


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
    activate_connection_scope()
    resolved = settings or Settings()
    app = FastAPI(title="Invoice Triage", lifespan=lifespan)
    app.state.settings = resolved
    app.state.intake_engine = (
        make_engine(resolved.intake_dsn) if resolved.intake_dsn else None
    )
    app.state.audit_engine = (
        make_engine(resolved.audit_dsn) if resolved.audit_dsn else None
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
        # Bind here, not in the endpoint thread: that thread receives a copy.
        settings: Settings = request.app.state.settings
        reviewer = authenticate(request) if settings.hosted else None
        request.state.reviewer = reviewer
        tokens = bind_scope(hosted=settings.hosted, reviewer=reviewer)
        try:
            return cast(Response, await call_next(request))
        finally:
            reset_scope(tokens)

    @app.get("/")
    def index(request: Request) -> Response:
        denied = _unsigned_response(request)
        if denied is not None:
            return denied
        demo = _prepare_demo(request)
        intake, _, _ = _engines(request.app)
        overview = list_overview(
            intake, attempt_id=None if demo is None else demo["attempt_id"]
        )
        if demo is not None:
            overview = {**overview, **demo}
        context = {
            "overview": overview,
            "attempts": list_demo_attempts(intake) if demo else [],
            "last_run": request.query_params.get("last_run"),
            "last_status": request.query_params.get("last_status"),
            "duplicate": request.query_params.get("duplicate"),
            "duplicate_ref": request.query_params.get("invoice_ref"),
            "duplicate_version": request.query_params.get("version"),
            "made_current": request.query_params.get("made_current"),
        }
        return _render(request, "index.html", context, overview)

    @app.post("/demo-attempts/start-fresh")
    def start_fresh(request: Request) -> Response:
        denied = _unsigned_response(request)
        if denied is not None:
            return denied
        if not request.app.state.settings.hosted:
            return _error(request, "not found", 404)
        reviewer = _reviewer_id(request)
        assert reviewer is not None
        _prepare_demo(request)
        intake, _, _ = _engines(request.app)
        started = start_fresh_attempt(intake, reviewer)
        set_attempt(started["attempt_id"])
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
        _prepare_demo(request)
        intake, _, audit = _engines(request.app)
        try:
            payload = read_demo_attempt(
                intake=intake, audit=audit, attempt_id=attempt_id
            )
        except AttemptNotFoundError as exc:
            return _error(request, str(exc), 404)
        if wants_json(request):
            return JSONResponse(cast(dict[str, Any], payload))
        return _render(
            request,
            "index.html",
            {"overview": payload, "read_only": True},
            cast(dict[str, Any], payload),
        )

    @app.post("/ingest")
    def ingest(request: Request, file: UploadFile) -> Response:
        demo = _prepare_mutation(request)
        if isinstance(demo, Response):
            return demo
        intake, _, _ = _engines(request.app)
        filename = file.filename or "upload.csv"
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return _index_error(
                request,
                intake,
                f"{filename}: upload exceeds {MAX_UPLOAD_BYTES} bytes",
                status_code=413,
            )
        try:
            submission = ingest_submission(intake, filename=filename, data=data)
        except ValueError as exc:
            return _index_error(request, intake, str(exc), status_code=400)
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
        demo = _prepare_mutation(request)
        if isinstance(demo, Response):
            return demo
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
        denied = _unsigned_response(request)
        if denied is not None:
            return denied
        _prepare_demo(request)
        intake, _, audit = _engines(request.app)
        try:
            case = get_case_detail(intake=intake, audit=audit, case_id=case_id)
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
        demo = _prepare_mutation(request)
        if isinstance(demo, Response):
            return demo
        intake, _, audit = _engines(request.app)
        try:
            outcome = resolve_case(
                intake=intake,
                audit=audit,
                case_id=case_id,
                attempt_id=None if demo is None else demo["attempt_id"],
            )
        except CaseNotFoundError as exc:
            return _error(request, str(exc), 404)
        if outcome.get("reason") == "attempt-read-only":
            if wants_json(request):
                return JSONResponse(
                    {**outcome, "detail": "attempt-read-only"}, status_code=409
                )
            return _error(request, "attempt-read-only", 409)
        if wants_json(request):
            return JSONResponse(cast(dict[str, Any], outcome))
        if outcome["resolved"]:
            return RedirectResponse(f"/cases/{case_id}", status_code=303)
        query = urlencode({"resolve_refused": str(outcome["reason"])})
        return RedirectResponse(f"/cases/{case_id}?{query}", status_code=303)

    return app


app = create_app()
