"""Experiment sender and archived run verifier; offline replay needs no credentials."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import Field, field_validator

from invoice_triage.deterministic import extract_fields
from invoice_triage.extraction import (
    RESULT,
    Failure,
    StrictModel,
    Success,
    canonical,
    digest,
    load_json,
    read_artifact,
    validate_result,
)
from invoice_triage.pdf_text import MAX_PDF_BYTES, PDFText, read_pdf

if TYPE_CHECKING:
    import httpx

MODEL = "gemini-3.5-flash-lite"
SDK_VERSION = "2.32.0"
TIMEOUT_MS = 60_000
HOME_NAME = "invoice-triage-pdf-experiment-v1"
ROLES = {"evaluation": 10, "development": 3, "tuning": 1, "preflight": 1}


class _Evidence(StrictModel):
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statement: str = Field(min_length=20, max_length=8000)

    @field_validator("url")
    @classmethod
    def primary_source(cls, value: str) -> str:
        if not re.fullmatch(
            r"https://(?:cloud\.google\.com|docs\.cloud\.google\.com|"
            r"ai\.google\.dev|googleapis\.github\.io)/[^\s]+",
            value,
        ):
            raise ValueError("accounting evidence needs a primary provider URL")
        return value


class _Slot(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    role: Literal["evaluation", "development", "tuning", "preflight"]
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_token_bound: int = Field(ge=1, le=12_000)
    token_count_evidence: _Evidence


class Plan(StrictModel):
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,62}$")
    location: Literal["global"]
    prompt: str = Field(min_length=1, max_length=32_000)
    slots: list[_Slot] = Field(min_length=15, max_length=15)


class Eligibility(StrictModel):
    kind: Literal["user-verified", "controlled-transport"]
    attested_by: str = Field(min_length=1, max_length=200)
    verified_at: str
    expires_at: str
    credit_expires_at: str
    eligible_credit_usd: str = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,9})?$")
    project: str
    location: Literal["global"]
    service: Literal["vertex-ai"]
    sku: str = Field(min_length=1, max_length=200)
    model: Literal["gemini-3.5-flash-lite"]
    input_usd_per_million: str = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,9})?$")
    output_usd_per_million: str = Field(pattern=r"^[0-9]{1,5}(?:\.[0-9]{1,9})?$")
    output_token_cap: int = Field(ge=1, le=65_536)
    includes_billed_reasoning: Literal[True]
    bounded_after_disconnect: Literal[True]
    no_out_of_pocket: Literal[True]
    evidence: list[_Evidence] = Field(min_length=1, max_length=10)

    @field_validator(
        "includes_billed_reasoning",
        "bounded_after_disconnect",
        "no_out_of_pocket",
        mode="before",
    )
    @classmethod
    def explicit_attestation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("accounting attestations require explicit boolean true")
        return value


class _ArchivedDocument(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def pdf(self) -> str: ...


class _ArchivedProvenance(Protocol):
    @property
    def frozen_at(self) -> str: ...

    @property
    def developers(self) -> Sequence[str]: ...

    @property
    def curator(self) -> str | None: ...

    @property
    def exposure(self) -> Sequence[str]: ...

    @property
    def reserved_status(self) -> Literal["not-established", "established"]: ...


class _ArchivedRun(Protocol):
    @property
    def state(self) -> str: ...

    @property
    def seal(self) -> str: ...

    @property
    def raw_files(self) -> Mapping[str, str]: ...

    @property
    def model_arm(self) -> str: ...

    @property
    def baseline_arm(self) -> str: ...

    @property
    def deterministic_sha256(self) -> str: ...

    @property
    def layout(self) -> str: ...


class _ArchivedRunManifest(Protocol):
    @property
    def scope(self) -> Literal["development", "reserved"]: ...

    @property
    def kind(self) -> Literal["offline-stub", "first-outputs"]: ...

    @property
    def documents(self) -> Sequence[_ArchivedDocument]: ...

    @property
    def assigned_count(self) -> int: ...

    @property
    def provenance(self) -> _ArchivedProvenance: ...

    @property
    def prompt(self) -> str: ...

    @property
    def run(self) -> _ArchivedRun | None: ...


def code_identity() -> str:
    """Return the pinned SHA-256 identity of the runner and shared extraction code."""
    root = Path(__file__).parent
    return digest(
        SDK_VERSION.encode()
        + b"".join(
            (root / name).read_bytes()
            for name in (
                "model_runner.py",
                "extraction.py",
                "pdf_text.py",
            )
        )
    )


def _shared_git_dir() -> Path:
    common = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], text=True, stderr=subprocess.DEVNULL
    ).strip()
    return Path(common).resolve()


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Any) -> None:
    # A crash between ledger and seal updates fails closed on the next command.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _save_state(home: Path, state: dict[str, Any]) -> None:
    _atomic_json(home / "state.json", state)
    _atomic_json(
        home.parent / (HOME_NAME + ".seal.json"),
        {
            "experiment_id": state["plan"]["experiment_id"],
            "plan_sha256": digest(canonical(state["plan"])),
            "receipt_sha256": digest(canonical(state["receipt"])),
            "state_sha256": digest(canonical(state)),
        },
    )


def _read_json(path: Path) -> Any:
    if path.is_symlink():
        raise ValueError("substituted state/artifact symlink")
    return load_json(read_artifact(path.parent, path.name))


def _checked_plan(raw: object) -> Plan:
    plan = Plan.model_validate(raw)
    if Counter(slot.role for slot in plan.slots) != ROLES:
        raise ValueError(
            "freeze ten evaluation, three development, tuning and preflight slots"
        )
    if len({slot.id for slot in plan.slots}) != 15:
        raise ValueError("slot IDs must be unique")
    scored = [slot for slot in plan.slots if slot.role in {"evaluation", "development"}]
    if (
        len({slot.document_id for slot in scored}) != 13
        or len({slot.source_sha256 for slot in scored}) != 13
    ):
        raise ValueError("evaluation and scored-development documents must be distinct")
    return plan


def _stamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("eligibility timestamps need a timezone")
    return parsed


def _checked_receipt(raw: object, plan: Plan) -> Eligibility:
    receipt = Eligibility.model_validate(raw)
    if Decimal(receipt.eligible_credit_usd) <= 0:
        raise ValueError("positive eligible unexpired credit is required")
    now = datetime.now(UTC)
    if (
        not _stamp(receipt.verified_at)
        <= now
        < min(_stamp(receipt.expires_at), _stamp(receipt.credit_expires_at))
    ):
        raise ValueError("eligibility/accounting receipt is expired or future-dated")
    if receipt.project != plan.project or receipt.location != plan.location:
        raise ValueError("receipt is not for this exact managed route")
    return receipt


def _charge(slot: _Slot, receipt: Eligibility) -> Decimal:
    return (
        (
            slot.input_token_bound * Decimal(receipt.input_usd_per_million)
            + receipt.output_token_cap * Decimal(receipt.output_usd_per_million)
        )
        / 1_000_000
    ).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING)


def _planned_charge(plan: Plan, receipt: Eligibility) -> Decimal:
    total = sum((_charge(slot, receipt) for slot in plan.slots), Decimal(0))
    if total * Decimal("1.25") >= 4 or total * Decimal("1.25") > min(
        Decimal(5), Decimal(receipt.eligible_credit_usd)
    ):
        raise ValueError(
            "entire batch plus 25% must be strictly below $4 "
            "within eligible credit and $5"
        )
    return total


def _load_state(home: Path) -> dict[str, Any]:
    if home.is_symlink() or not (home / "state.json").is_file():
        raise ValueError(
            "allowance state is missing or substituted; no send is authorized"
        )
    state = _read_json(home / "state.json")
    seal = _read_json(home.parent / (HOME_NAME + ".seal.json"))
    if not isinstance(state, dict) or not isinstance(seal, dict):
        raise ValueError("corrupt allowance state")
    if seal != {
        "experiment_id": state["plan"]["experiment_id"],
        "plan_sha256": digest(canonical(state["plan"])),
        "receipt_sha256": digest(canonical(state["receipt"])),
        "state_sha256": digest(canonical(state)),
    }:
        raise ValueError("allowance state/identity seal mismatch")
    plan = _checked_plan(state["plan"])
    if state["code_sha256"] != code_identity():
        raise ValueError("frozen runner/contract changed; no new allowance or send")
    slots = {slot.id for slot in plan.slots}
    if not isinstance(state["attempts"], dict) or not set(state["attempts"]) <= slots:
        raise ValueError("allowance contains unknown or excessive sends")
    for name, attempt in state["attempts"].items():
        if attempt["status"] not in {"reserved", "complete", "failure"}:
            raise ValueError("invalid reservation status")
        if attempt.get("raw_sha256") is not None:
            if digest(read_artifact(home, f"{name}.raw.json")) != attempt["raw_sha256"]:
                raise ValueError("first raw response hash mismatch")
    return state


def request_payload(plan: Plan, receipt: Eligibility, text: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "contents": text,
        "config": {
            "system_instruction": plan.prompt,
            "candidate_count": 1,
            "thinking_config": {"thinking_level": "MINIMAL"},
            "max_output_tokens": receipt.output_token_cap,
            "response_mime_type": "application/json",
            "response_json_schema": RESULT.json_schema(),
            "automatic_function_calling": {"disable": True},
            "automatic_continuation": False,
        },
    }


def _decode_raw_envelope(
    raw: bytes, pages: list[str]
) -> tuple[Success | Failure, dict[str, Any] | None, str | None]:
    body_dict: dict[str, Any] | None = None
    try:
        body = load_json(raw)
        if not isinstance(body, dict):
            raise ValueError("raw response root must be a JSON object")
        body_dict = body
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise ValueError("nonfinal or multiple candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict) or candidate.get("finishReason") != "STOP":
            raise ValueError("nonfinal candidate")
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise ValueError("missing candidate content")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError("empty content parts")
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or (set(part) - {"text", "thoughtSignature"}):
                raise ValueError("non-text or tool envelope")
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("non-text envelope")
            texts.append(text)
        decoded = validate_result(load_json("".join(texts).encode()), pages)
        return decoded, body_dict, None
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return (
            Failure(
                version="1",
                status="failure",
                stage="validate",
                code="invalid_envelope",
            ),
            body_dict,
            type(exc).__name__,
        )


def _dispatch(
    home: Path,
    state: dict[str, Any],
    slot: _Slot,
    pdf: PDFText,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    import httpx
    from google import genai
    from google.genai import types
    from google.oauth2.credentials import Credentials

    plan = _checked_plan(state["plan"])
    receipt = _checked_receipt(state["receipt"], plan)
    attempt: dict[str, Any] = state["attempts"][slot.id]
    path = home / f"{slot.id}.raw.json"

    def preserve(response: httpx.Response) -> None:
        raw = response.read()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(home)

    started = time.monotonic()
    result: Any = Failure(
        version="1", status="failure", stage="extract", code="transport_failure"
    )
    try:
        with httpx.Client(
            transport=transport,
            timeout=TIMEOUT_MS / 1000,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"response": [preserve]},
        ) as http_client:
            options = types.HttpOptions(
                base_url="https://aiplatform.googleapis.com",
                api_version="v1",
                timeout=TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
                httpx_client=http_client,
            )
            kwargs: dict[str, Any] = {
                "vertexai": True,
                "project": plan.project,
                "location": plan.location,
                "http_options": options,
            }
            if transport is not None:
                kwargs["credentials"] = Credentials(  # type: ignore[no-untyped-call]
                    token="controlled-transport-only"
                )
            with genai.Client(**kwargs) as client:
                payload = request_payload(plan, receipt, pdf.text)
                config = types.GenerateContentConfig(**payload["config"])
                config.thinking_config = types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL
                )
                client.models.generate_content(
                    model=MODEL, contents=pdf.text, config=config
                )
    except Exception as exc:
        # Provider errors may contain sensitive URLs; retain the type, not its text.
        attempt["error_type"] = type(exc).__name__
        if path.exists():
            result = Failure(
                version="1", status="failure", stage="validate", code="invalid_envelope"
            )
    if path.exists():
        raw = path.read_bytes()
        attempt["raw_sha256"] = digest(raw)
        decoded, body, decode_error = _decode_raw_envelope(raw, pdf.pages)
        if "error_type" not in attempt:
            result = decoded
            if decode_error is not None:
                attempt["error_type"] = decode_error
        if body is not None:
            attempt["returned_model"] = body.get("modelVersion")
            attempt["usage"] = body.get("usageMetadata")
    attempt.update(
        status="failure" if isinstance(result, Failure) else "complete",
        result=result.model_dump(mode="json"),
        elapsed_seconds=time.monotonic() - started,
        completed_at=datetime.now(UTC).isoformat(),
        actual_cost_usd=None,
    )
    _save_state(home, state)
    return attempt


def verify_archived_run(
    manifest: _ArchivedRunManifest,
    artifacts: Mapping[str, bytes],
    arms: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify saved experiment archive state, seal, accounting, and raw envelopes."""
    run = manifest.run
    if run is None:
        raise ValueError("archived run verification requires saved run metadata")
    if (
        manifest.kind != "first-outputs"
        or {run.model_arm, run.baseline_arm} != set(arms)
        or run.model_arm == run.baseline_arm
    ):
        raise ValueError("first-output analysis must name its two distinct arms")
    provenance = manifest.provenance
    if manifest.scope == "reserved" and (
        manifest.assigned_count != 10
        or not run.layout
        or not provenance.curator
        or provenance.curator in provenance.developers
        or provenance.reserved_status != "established"
        or not provenance.exposure
    ):
        raise ValueError(
            "ten-document one-layout analysis requires independent curation/exposure"
        )
    if manifest.scope == "development" and manifest.assigned_count != 3:
        raise ValueError(
            "first-output development analysis uses the three frozen scored slots"
        )
    state, seal = load_json(artifacts[run.state]), load_json(artifacts[run.seal])
    if seal != {
        "experiment_id": state["plan"]["experiment_id"],
        "plan_sha256": digest(canonical(state["plan"])),
        "receipt_sha256": digest(canonical(state["receipt"])),
        "state_sha256": digest(canonical(state)),
    }:
        raise ValueError("saved allowance seal mismatch")
    plan = _checked_plan(state["plan"])
    receipt = Eligibility.model_validate(state["receipt"])
    if state["planned_reservation_usd"] != str(_planned_charge(plan, receipt)):
        raise ValueError("saved reservation total differs from frozen accounting")
    if (
        receipt.project != plan.project
        or receipt.location != plan.location
        or Decimal(receipt.eligible_credit_usd) <= 0
    ):
        raise ValueError("saved eligibility does not cover the exact experiment")
    if state["code_sha256"] != code_identity() or run.deterministic_sha256 != digest(
        Path(__file__).with_name("deterministic.py").read_bytes()
    ):
        raise ValueError("frozen extractor or runner code changed")
    if artifacts[manifest.prompt].decode() != plan.prompt:
        raise ValueError("frozen prompt differs from runner plan")
    role = "evaluation" if manifest.scope == "reserved" else "development"
    slots = {s.document_id: s for s in plan.slots if s.role == role}
    if set(slots) != {d.id for d in manifest.documents}:
        raise ValueError("analysis documents differ from the frozen slot assignment")
    attempts = state["attempts"]
    known = {s.id: s for s in plan.slots}
    if not set(attempts) <= set(known):
        raise ValueError("saved allowance has unknown sends")
    if set(run.raw_files) != {
        name for name, a in attempts.items() if a.get("raw_sha256") is not None
    }:
        raise ValueError("first raw response inventory differs from all consumed slots")
    frozen_at = _stamp(provenance.frozen_at)
    for name, attempt in attempts.items():
        if attempt["status"] not in {"reserved", "complete", "failure"}:
            raise ValueError("invalid saved send status")
        if attempt["maximum_charge_usd"] != str(_charge(known[name], receipt)):
            raise ValueError("attempt charge differs from frozen accounting")
        if attempt.get("elapsed_seconds") is not None and (
            not math.isfinite(attempt["elapsed_seconds"])
            or attempt["elapsed_seconds"] < 0
        ):
            raise ValueError("invalid extraction elapsed time")
        at = _stamp(attempt["reserved_at"])
        if at < frozen_at or not _stamp(receipt.verified_at) <= at < min(
            _stamp(receipt.expires_at), _stamp(receipt.credit_expires_at)
        ):
            raise ValueError(
                "send precedes freeze or lacks eligible accounting at dispatch"
            )
        if (
            attempt.get("raw_sha256") is not None
            and digest(artifacts[run.raw_files[name]]) != attempt["raw_sha256"]
        ):
            raise ValueError("first raw response hash mismatch")
    for doc in manifest.documents:
        source = artifacts[doc.pdf]
        slot = slots[doc.id]
        pdf = read_pdf(source)
        if digest(source) != slot.source_sha256:
            raise ValueError("saved source differs from frozen runner slot")
        if not isinstance(pdf, PDFText):
            if slot.id in attempts or any(
                arm[doc.id] != pdf.model_dump(mode="json") for arm in arms.values()
            ):
                raise ValueError(
                    "shared adapter failure must remain in both arms without a send"
                )
            continue
        expected_baseline = extract_fields(pdf).model_dump(mode="json")
        if arms[run.baseline_arm][doc.id] != expected_baseline:
            raise ValueError("baseline differs from frozen deterministic extraction")
        request_hash = digest(canonical(request_payload(plan, receipt, pdf.text)))
        if request_hash != slot.request_sha256:
            raise ValueError("complete request differs from counted frozen request")
        attempt = attempts.get(slot.id)
        expected: Any = Failure(
            version="1", status="failure", stage="extract", code="not_sent"
        )
        if attempt is not None:
            for key, value in {
                "source_sha256": slot.source_sha256,
                "request_sha256": request_hash,
                "text_sha256": pdf.text_sha256,
                "prompt_sha256": digest(plan.prompt.encode()),
                "schema_sha256": digest(canonical(RESULT.json_schema())),
                "requested_model": receipt.model,
            }.items():
                if attempt[key] != value:
                    raise ValueError("attempt identity differs from frozen request")
            if attempt["status"] == "reserved":
                expected = Failure(
                    version="1",
                    status="failure",
                    stage="extract",
                    code="uncertain_send",
                )
            else:
                expected = validate_result(attempt["result"], pdf.pages)
                if attempt.get("raw_sha256") is not None:
                    first, body, _ = _decode_raw_envelope(
                        artifacts[run.raw_files[slot.id]], pdf.pages
                    )
                    if first != expected:
                        raise ValueError("saved result differs from first raw response")
                    if body is not None and (
                        attempt.get("returned_model") != body.get("modelVersion")
                        or attempt.get("usage") != body.get("usageMetadata")
                    ):
                        raise ValueError(
                            "returned version/usage differs from first raw response"
                        )
                elif not isinstance(expected, Failure):
                    raise ValueError("successful output lacks a first raw response")
        if arms[run.model_arm][doc.id] != expected.model_dump(mode="json"):
            raise ValueError(
                "model arm differs from first result, including failed/uncertain sends"
            )
    return {
        "kind": receipt.kind,
        "experiment_id": plan.experiment_id,
        "layout": run.layout,
        "requested_model": receipt.model,
        "sdk": f"google-genai=={SDK_VERSION}",
        "runner_code_sha256": state["code_sha256"],
        "deterministic_sha256": run.deterministic_sha256,
        "plan_sha256": seal["plan_sha256"],
        "receipt_sha256": seal["receipt_sha256"],
        "planned_reservation_usd": state["planned_reservation_usd"],
        "attempts": attempts,
        "actual_cost_usd": None,
        "evidence_limit": (
            "Hashes establish consistency; curation, exposure and account "
            "eligibility remain human attestations. Replay is not a fresh provider run."
        ),
    }


def main(
    argv: list[str] | None = None,
    *,
    common_dir: Path | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "send", "audit", "replay"])
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--slot")
    parser.add_argument("--pdf", type=Path)
    args = parser.parse_args(argv)
    common: Path | None = None
    try:
        common = (common_dir or _shared_git_dir()).resolve(strict=True)
        home = common / HOME_NAME
        lock_fd = os.open(
            common / (HOME_NAME + ".lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(lock_fd, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "prepare":
                if home.exists() or (common / (HOME_NAME + ".seal.json")).exists():
                    raise ValueError(
                        "experiment already exists; never recreate a missing allowance"
                    )
                if args.plan is None or args.receipt is None:
                    raise ValueError(
                        "a frozen plan and sanitized user eligibility/accounting "
                        "receipt are required"
                    )
                plan = _checked_plan(_read_json(args.plan))
                receipt = _checked_receipt(_read_json(args.receipt), plan)
                total = _planned_charge(plan, receipt)
                home.mkdir(mode=0o700)
                state: dict[str, Any] = {
                    "version": "1",
                    "plan": plan.model_dump(mode="json"),
                    "receipt": receipt.model_dump(mode="json"),
                    "code_sha256": code_identity(),
                    "planned_reservation_usd": str(total),
                    "attempts": {},
                }
                _save_state(home, state)
                output: Any = {
                    "slots": ROLES,
                    "planned_reservation_usd": str(total),
                    "with_25_percent_usd": str(total * Decimal("1.25")),
                    "sends": 0,
                    "kind": receipt.kind,
                    "experiment_home": str(home),
                }
            else:
                state = _load_state(home)
                plan = _checked_plan(state["plan"])
                if args.command == "audit":
                    output = {
                        "experiment_id": plan.experiment_id,
                        "started": len(state["attempts"]),
                        "slots": ROLES,
                        "planned_reservation_usd": state["planned_reservation_usd"],
                        "attempts": state["attempts"],
                    }
                elif args.command == "replay":
                    output = state["attempts"][args.slot]
                    if output["status"] == "reserved":
                        raise ValueError(
                            "unfinished send retains its reservation; never resend it"
                        )
                    RESULT.validate_python(output["result"])
                else:
                    receipt = _checked_receipt(state["receipt"], plan)
                    _planned_charge(plan, receipt)
                    if transport is None and receipt.kind != "user-verified":
                        raise ValueError(
                            "controlled test evidence cannot authorize a live send"
                        )
                    if version("google-genai") != SDK_VERSION:
                        raise ValueError("SDK differs from the frozen version")
                    if args.slot in state["attempts"]:
                        raise ValueError(
                            "slot already reserved/consumed; "
                            "no retry or replacement send"
                        )
                    slot = next(
                        (slot for slot in plan.slots if slot.id == args.slot), None
                    )
                    if slot is None or args.pdf is None:
                        raise ValueError(
                            "a predeclared slot and its exact PDF are required"
                        )
                    with args.pdf.open("rb") as handle:
                        data = handle.read(MAX_PDF_BYTES + 1)
                    if digest(data) != slot.source_sha256:
                        raise ValueError("source differs from frozen slot")
                    pdf = read_pdf(data)
                    if not isinstance(pdf, PDFText):
                        raise ValueError("PDF refused before dispatch")
                    payload = request_payload(plan, receipt, pdf.text)
                    if digest(canonical(payload)) != slot.request_sha256:
                        raise ValueError(
                            "complete prompt/schema/text request "
                            "differs from counted input"
                        )
                    state["attempts"][slot.id] = {
                        "status": "reserved",
                        "requested_model": MODEL,
                        "reserved_at": datetime.now(UTC).isoformat(),
                        "maximum_charge_usd": str(_charge(slot, receipt)),
                        "source_sha256": slot.source_sha256,
                        "request_sha256": slot.request_sha256,
                        "text_sha256": pdf.text_sha256,
                        "prompt_sha256": digest(plan.prompt.encode()),
                        "schema_sha256": digest(canonical(RESULT.json_schema())),
                        "actual_cost_usd": None,
                    }
                    _save_state(home, state)
                    output = _dispatch(home, state, slot, pdf, transport)
            print(json.dumps(output, indent=2, sort_keys=True, allow_nan=False))
            return 2 if output.get("status") == "failure" else 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        PackageNotFoundError,
        subprocess.CalledProcessError,
    ) as exc:
        reason = "runner refused: " + str(exc)
        if args.command == "prepare" and common is not None:
            _atomic_json(
                common / (HOME_NAME + ".last-refusal.json"),
                {"at": datetime.now(UTC).isoformat(), "reason": reason},
            )
        print(reason, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
