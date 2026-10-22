"""One finite experiment sender; ordinary replay needs no provider or credentials."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, field_validator

from invoice_triage.evaluation import load_json, read_artifact
from invoice_triage.extraction import RESULT, Failure, StrictModel, validate_result
from invoice_triage.pdf_text import MAX_PDF_BYTES, PDFText, read_pdf

if TYPE_CHECKING:
    import httpx

MODEL = "gemini-3.5-flash-lite"
SDK_VERSION = "2.32.0"
TIMEOUT_MS = 60_000
HOME_NAME = "invoice-triage-pdf-experiment-v1"
ROLES = {"evaluation": 10, "development": 3, "tuning": 1, "preflight": 1}


class Evidence(StrictModel):
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


class Slot(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    role: Literal["evaluation", "development", "tuning", "preflight"]
    document_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_token_bound: int = Field(ge=1, le=12_000)
    token_count_evidence: Evidence


class Plan(StrictModel):
    experiment_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,62}$")
    location: Literal["global"]
    prompt: str = Field(min_length=1, max_length=32_000)
    slots: list[Slot] = Field(min_length=15, max_length=15)


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
    evidence: list[Evidence] = Field(min_length=1, max_length=10)

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


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code_identity() -> str:
    root = Path(__file__).parent
    return digest(
        SDK_VERSION.encode()
        + b"".join(
            (root / name).read_bytes()
            for name in (
                "model_runner.py",
                "evaluation.py",
                "extraction.py",
                "pdf_text.py",
            )
        )
    )


def shared_git_dir() -> Path:
    common = subprocess.check_output(
        ["git", "rev-parse", "--git-common-dir"], text=True, stderr=subprocess.DEVNULL
    ).strip()
    return Path(common).resolve()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: Any) -> None:
    # A crash between ledger and seal updates fails closed on the next command.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def save_state(home: Path, state: dict[str, Any]) -> None:
    atomic_json(home / "state.json", state)
    atomic_json(
        home.parent / (HOME_NAME + ".seal.json"),
        {
            "experiment_id": state["plan"]["experiment_id"],
            "plan_sha256": digest(canonical(state["plan"])),
            "receipt_sha256": digest(canonical(state["receipt"])),
            "state_sha256": digest(canonical(state)),
        },
    )


def read_json(path: Path) -> Any:
    if path.is_symlink():
        raise ValueError("substituted state/artifact symlink")
    return load_json(read_artifact(path.parent, path.name))


def checked_plan(raw: object) -> Plan:
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


def stamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError("eligibility timestamps need a timezone")
    return parsed


def checked_receipt(raw: object, plan: Plan) -> Eligibility:
    receipt = Eligibility.model_validate(raw)
    if Decimal(receipt.eligible_credit_usd) <= 0:
        raise ValueError("positive eligible unexpired credit is required")
    now = datetime.now(UTC)
    if (
        not stamp(receipt.verified_at)
        <= now
        < min(stamp(receipt.expires_at), stamp(receipt.credit_expires_at))
    ):
        raise ValueError("eligibility/accounting receipt is expired or future-dated")
    if receipt.project != plan.project or receipt.location != plan.location:
        raise ValueError("receipt is not for this exact managed route")
    return receipt


def charge(slot: Slot, receipt: Eligibility) -> Decimal:
    return (
        (
            slot.input_token_bound * Decimal(receipt.input_usd_per_million)
            + receipt.output_token_cap * Decimal(receipt.output_usd_per_million)
        )
        / 1_000_000
    ).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING)


def planned_charge(plan: Plan, receipt: Eligibility) -> Decimal:
    total = sum((charge(slot, receipt) for slot in plan.slots), Decimal(0))
    if total * Decimal("1.25") >= 4 or total * Decimal("1.25") > min(
        Decimal(5), Decimal(receipt.eligible_credit_usd)
    ):
        raise ValueError(
            "entire batch plus 25% must be strictly below $4 "
            "within eligible credit and $5"
        )
    return total


def load_state(home: Path) -> dict[str, Any]:
    if home.is_symlink() or not (home / "state.json").is_file():
        raise ValueError(
            "allowance state is missing or substituted; no send is authorized"
        )
    state = read_json(home / "state.json")
    seal = read_json(home.parent / (HOME_NAME + ".seal.json"))
    if not isinstance(state, dict) or not isinstance(seal, dict):
        raise ValueError("corrupt allowance state")
    if seal != {
        "experiment_id": state["plan"]["experiment_id"],
        "plan_sha256": digest(canonical(state["plan"])),
        "receipt_sha256": digest(canonical(state["receipt"])),
        "state_sha256": digest(canonical(state)),
    }:
        raise ValueError("allowance state/identity seal mismatch")
    plan = checked_plan(state["plan"])
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


def dispatch(
    home: Path,
    state: dict[str, Any],
    slot: Slot,
    pdf: PDFText,
    transport: httpx.BaseTransport | None,
) -> dict[str, Any]:
    import httpx
    from google import genai
    from google.genai import types
    from google.oauth2.credentials import Credentials

    plan = checked_plan(state["plan"])
    receipt = checked_receipt(state["receipt"], plan)
    attempt: dict[str, Any] = state["attempts"][slot.id]
    path = home / f"{slot.id}.raw.json"

    def preserve(response: httpx.Response) -> None:
        raw = response.read()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(home)

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
        body = load_json(path.read_bytes())
        candidates = body["candidates"]
        if len(candidates) != 1 or candidates[0]["finishReason"] != "STOP":
            raise ValueError("nonfinal or multiple candidates")
        parts = candidates[0]["content"]["parts"]
        if not parts or any(
            set(part) - {"text", "thoughtSignature"}
            or not isinstance(part.get("text"), str)
            for part in parts
        ):
            raise ValueError("non-text or tool envelope")
        result = validate_result(
            load_json("".join(part["text"] for part in parts).encode()), pdf.pages
        )
    except Exception as exc:
        # Provider errors may contain sensitive URLs; retain the type, not its text.
        attempt["error_type"] = type(exc).__name__
        if path.exists():
            result = Failure(
                version="1", status="failure", stage="validate", code="invalid_envelope"
            )
    attempt.update(
        status="failure" if isinstance(result, Failure) else "complete",
        result=result.model_dump(mode="json"),
        elapsed_seconds=time.monotonic() - started,
        completed_at=datetime.now(UTC).isoformat(),
        actual_cost_usd=None,
    )
    if path.exists():
        raw = path.read_bytes()
        attempt["raw_sha256"] = digest(raw)
        try:
            body = load_json(raw)
            attempt["returned_model"] = body.get("modelVersion")
            attempt["usage"] = body.get("usageMetadata")
        except (ValueError, AttributeError):
            pass
    save_state(home, state)
    return attempt


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
        common = (common_dir or shared_git_dir()).resolve(strict=True)
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
                plan = checked_plan(read_json(args.plan))
                receipt = checked_receipt(read_json(args.receipt), plan)
                total = planned_charge(plan, receipt)
                home.mkdir(mode=0o700)
                state: dict[str, Any] = {
                    "version": "1",
                    "plan": plan.model_dump(mode="json"),
                    "receipt": receipt.model_dump(mode="json"),
                    "code_sha256": code_identity(),
                    "planned_reservation_usd": str(total),
                    "attempts": {},
                }
                save_state(home, state)
                output: Any = {
                    "slots": ROLES,
                    "planned_reservation_usd": str(total),
                    "with_25_percent_usd": str(total * Decimal("1.25")),
                    "sends": 0,
                    "kind": receipt.kind,
                    "experiment_home": str(home),
                }
            else:
                state = load_state(home)
                plan = checked_plan(state["plan"])
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
                    receipt = checked_receipt(state["receipt"], plan)
                    planned_charge(plan, receipt)
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
                        "maximum_charge_usd": str(charge(slot, receipt)),
                        "source_sha256": slot.source_sha256,
                        "request_sha256": slot.request_sha256,
                        "text_sha256": pdf.text_sha256,
                        "prompt_sha256": digest(plan.prompt.encode()),
                        "schema_sha256": digest(canonical(RESULT.json_schema())),
                        "actual_cost_usd": None,
                    }
                    save_state(home, state)
                    output = dispatch(home, state, slot, pdf, transport)
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
            atomic_json(
                common / (HOME_NAME + ".last-refusal.json"),
                {"at": datetime.now(UTC).isoformat(), "reason": reason},
            )
        print(reason, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
