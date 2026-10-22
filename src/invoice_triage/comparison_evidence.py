"""Offline consistency checks on first outputs, freeze and accounting receipts."""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path
from typing import Any

from invoice_triage.deterministic import extract_fields
from invoice_triage.evaluation import Manifest, load_json
from invoice_triage.extraction import RESULT, Failure, validate_result
from invoice_triage.model_runner import (
    Eligibility,
    canonical,
    charge,
    checked_plan,
    code_identity,
    digest,
    planned_charge,
    request_payload,
    stamp,
)
from invoice_triage.pdf_text import PDFText, read_pdf


def check_run(
    manifest: Manifest, artifacts: dict[str, bytes], arms: dict[str, Any]
) -> dict[str, Any]:
    run = manifest.run
    assert run is not None
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
    plan = checked_plan(state["plan"])
    receipt = Eligibility.model_validate(state["receipt"])
    if state["planned_reservation_usd"] != str(planned_charge(plan, receipt)):
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
    frozen_at = stamp(provenance.frozen_at)
    for name, attempt in attempts.items():
        if attempt["status"] not in {"reserved", "complete", "failure"}:
            raise ValueError("invalid saved send status")
        if attempt["maximum_charge_usd"] != str(charge(known[name], receipt)):
            raise ValueError("attempt charge differs from frozen accounting")
        if attempt.get("elapsed_seconds") is not None and (
            not math.isfinite(attempt["elapsed_seconds"])
            or attempt["elapsed_seconds"] < 0
        ):
            raise ValueError("invalid extraction elapsed time")
        at = stamp(attempt["reserved_at"])
        if at < frozen_at or not stamp(receipt.verified_at) <= at < min(
            stamp(receipt.expires_at), stamp(receipt.credit_expires_at)
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
                    body = None
                    try:
                        body = load_json(artifacts[run.raw_files[slot.id]])
                        candidates = body["candidates"]
                        if (
                            len(candidates) != 1
                            or candidates[0]["finishReason"] != "STOP"
                        ):
                            raise ValueError("nonfinal candidates")
                        parts = candidates[0]["content"]["parts"]
                        if not parts or any(
                            set(p) - {"text", "thoughtSignature"}
                            or not isinstance(p.get("text"), str)
                            for p in parts
                        ):
                            raise ValueError("non-text envelope")
                        first = validate_result(
                            load_json("".join(p["text"] for p in parts).encode()),
                            pdf.pages,
                        )
                    except (ValueError, KeyError, TypeError):
                        first = Failure(
                            version="1",
                            status="failure",
                            stage="validate",
                            code="invalid_envelope",
                        )
                    if first != expected:
                        raise ValueError("saved result differs from first raw response")
                    if isinstance(body, dict) and (
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
        "sdk": "google-genai==2.32.0",
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
