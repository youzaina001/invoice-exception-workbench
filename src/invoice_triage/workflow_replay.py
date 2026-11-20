"""Replay frozen saved outputs through explicit PDF confirmation and real checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from invoice_triage.evaluation import load_bundle, replay
from invoice_triage.extraction import (
    Failure,
    StrictModel,
    load_json,
    read_artifact,
    validate_result,
)
from invoice_triage.governance import (
    configure_governance,
    core_identity,
    provision_governance_schema,
)

POLICY = "confirm-unchanged-then-declared-source-replacement-v1"
PINNED_CORE = "5d602428a93e5eee733b4ed1ffd06af4cbba250b"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def code_identity() -> str:
    root = Path(__file__).resolve().parent
    return sha(b"".join(path.read_bytes() for path in sorted(root.glob("*.py"))))


class Correction(StrictModel):
    pdf: str
    arms: dict[str, str]
    gold: str


class Observation(StrictModel):
    document: str
    arm: str
    active_seconds: float = Field(gt=0, allow_inf_nan=False)
    method: str = Field(min_length=1, max_length=500)


class Plan(StrictModel):
    version: Literal["1"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_revision: Literal["5d602428a93e5eee733b4ed1ffd06af4cbba250b"]
    policy: Literal["confirm-unchanged-then-declared-source-replacement-v1"]
    expected: dict[str, Literal["refused", "no_case", "open", "resolved"]]
    corrections: dict[str, Correction]
    hashes: dict[str, str]
    observations: list[Observation]
    evidence_class: Literal["authored-example", "first-outputs", "fault-injection"]


def execute(
    client: Any,
    source: bytes,
    raw: Any,
    *,
    arm: str,
    run_id: str,
    kind: str,
    correction: tuple[bytes, Any] | None,
) -> dict[str, Any]:
    """Declared actions receive no gold, expected disposition or scenario labels."""
    from invoice_triage.pdf_text import PDFText, read_pdf

    started = time.monotonic()
    result: dict[str, Any] = {
        "outcome": "refused",
        "accepted": False,
        "fields": None,
        "confirmation": "not-confirmable",
        "review_seconds": None,
        "events": [],
    }

    def submit(data: bytes, output: Any) -> tuple[Any, Any]:
        converted = read_pdf(data)
        pages = converted.pages if isinstance(converted, PDFText) else []
        try:
            checked = validate_result(output, pages)
        except ValueError:
            checked = Failure(
                version="1", status="failure", stage="validate", code="invalid_result"
            )
        saved = {
            "version": "1",
            "kind": kind,
            "arm": arm,
            "run_id": run_id,
            "source_sha256": sha(data),
            "result": checked.model_dump(mode="json"),
        }
        response = client.post(
            "/pdf-drafts",
            headers={"Accept": "application/json"},
            files={
                "file": ("synthetic.pdf", data, "application/pdf"),
                "extraction": (
                    "saved.json",
                    json.dumps(saved).encode(),
                    "application/json",
                ),
            },
        )
        if response.status_code != 201:
            result["events"].append(
                {"action": "upload", "status": response.status_code}
            )
            return None, None
        draft = response.json()
        payload = {
            "revision": draft["revision"],
            "binding": draft["binding"],
            "fields": draft["fields"],
        }
        base = f"/pdf-drafts/{draft['id']}"
        for action in ("confirm", "intake"):
            response = client.post(
                base + "/" + action,
                headers={"Accept": "application/json"},
                json=payload,
            )
            result["events"].append(
                {
                    "action": action,
                    "status": response.status_code,
                    "draft_id": draft["id"],
                    "revision": draft["revision"],
                    "binding": draft["binding"],
                }
            )
            if response.status_code != 200:
                return draft, None
            if action == "confirm":
                result["confirmation"] = "explicit-scripted"
            else:
                result["events"][-1]["submission"] = response.json()
        result["accepted"] = True
        result["fields"] = draft["fields"]
        response = client.post("/runs", headers={"Accept": "application/json"})
        response.raise_for_status()
        run = response.json()
        if run["status"] not in ("pass", "fail"):
            result["events"].append({"action": "checks", "status": run["status"]})
        result["events"].append(
            {
                "action": "checks",
                "status": run["status"],
                "case_created": run["case_id"] is not None,
                "case_id": run["case_id"],
                "run_id": run["run_id"],
                "report_id": run["report_id"],
            }
        )
        return draft, run

    _, initial = submit(source, raw)
    if initial is not None:
        case_id = initial["case_id"]
        result["outcome"] = "no_case" if case_id is None else "open"
        if case_id is not None and correction is not None:
            _, corrected = submit(*correction)
            if corrected is not None:
                response = client.post(
                    f"/cases/{case_id}/resolve", headers={"Accept": "application/json"}
                )
                response.raise_for_status()
                resolution = response.json()
                result["events"].append(
                    {
                        "action": "resolve",
                        "resolved": resolution["resolved"],
                        "reason": resolution.get("reason"),
                    }
                )
                detail = client.get(
                    f"/cases/{case_id}", headers={"Accept": "application/json"}
                )
                detail.raise_for_status()
                result["outcome"] = detail.json()["status"]
    result["journey_seconds"] = time.monotonic() - started
    return result


def agrees(fields: Any, expected: Any) -> bool:
    return fields is not None and all(
        fields[name]["state"] == field["state"]
        and fields[name]["value"] == field["value"]
        for name, field in expected.items()
    )


def ratio(wrong: int, total: int) -> dict[str, Any]:
    return {
        "wrong": wrong,
        "total": total,
        "ratio": f"{wrong}/{total}" if total else "N/A",
    }


def summarize(rows: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    result = {}
    for arm in arms:
        records = [r for r in rows if r["arm"] == arm]
        no_case = [r for r in records if r["outcome"] == "no_case"]
        resolved = [r for r in records if r["outcome"] == "resolved"]
        cleared = sum(
            r["correct"] and r["outcome"] in ("no_case", "resolved") for r in records
        )
        result[arm] = {
            "assigned": len(records),
            "outcomes": dict(Counter(r["outcome"] for r in records)),
            "expected": dict(Counter(r["expected"] for r in records)),
            "incorrect_no_case": ratio(
                sum(not r["correct"] for r in no_case), len(no_case)
            ),
            "incorrect_resolutions": ratio(
                sum(not r["correct"] for r in resolved), len(resolved)
            ),
            "correctly_processed": sum(r["correct"] for r in records),
            "correctly_cleared": cleared,
            "actual_cost_usd": None,
            "cost_per_correctly_cleared": None if cleared else "N/A",
            "by_expected": {
                disposition: {
                    "assigned": sum(r["expected"] == disposition for r in records),
                    "incorrect_no_case": ratio(
                        sum(
                            not r["correct"]
                            for r in no_case
                            if r["expected"] == disposition
                        ),
                        sum(r["expected"] == disposition for r in no_case),
                    ),
                    "incorrect_resolutions": ratio(
                        sum(
                            not r["correct"]
                            for r in resolved
                            if r["expected"] == disposition
                        ),
                        sum(r["expected"] == disposition for r in resolved),
                    ),
                }
                for disposition in ("open", "resolved")
            },
        }
    return result


def render(report: dict[str, Any]) -> str:
    raw = report["raw"]
    lines = [
        "# Invoice Triage saved-output report",
        "",
        (
            "Synthetic data, one held-out layout; archived first outputs. "
            "Replay establishes no fresh live integration."
            if raw["scope"] == "reserved"
            else "Synthetic development data; no held-out result is established."
        ),
        f"Evidence class: {report['evidence_class']}. "
        f"Saved live-run evidence: {report['live_comparison']}.",
        "",
        "| Raw extraction arm | All-five value/state agreement |",
        "| --- | --- |",
    ]
    for arm, stats in raw["arms"].items():
        counts = stats["primary"]
        lines.append(
            f"| {arm} | {counts['correct']}/{counts['total']} {raw['scope']} |"
        )
    lines += [
        "",
        raw["comparison"],
        "Equal primary counts are a tie. No general model winner is established. "
        "S10 tests field fidelity, not safety certification.",
        "",
        "Scripted review policy: confirm unchanged saved fields when the common "
        "gate permits; only an actual open case receives its predeclared source "
        "replacement, fresh confirmation/checks and explicit resolution. "
        "Gold is used afterward for scoring.",
        "",
        "| Arm | Document | Expected | Confirmed workflow | "
        "All-five final agreement | Explanation |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['arm']} | {row['document']} | {row['expected']} | "
            f"{row['outcome']} | {row['fields_agree']} | {row['explanation']} |"
        )
    lines += [
        "",
        "Confirmation, passing completeness and case resolution do not establish "
        "invoice truth or approve payment.",
        "",
        "No Operator observations; review duration and time saved are unavailable."
        if not report["operator_observations"]
        else (
            "Operator observations are supplied separately below; active time "
            "excludes unattended and supplier-correction wait. Scripted journey "
            "duration is not human review time."
        ),
        "",
        "Extraction latency is retained per attempt when measured; it is "
        "unavailable for authored outputs. Scripted journey latency is separate "
        "in JSON. Actual billed cost is unavailable; reservations are upper "
        "bounds and include failed/uncertain sends. Every assigned document "
        "remains in the denominator. Empty and zero-success ratios are N/A.",
        "",
        "The CSV flow is preserved. Local PDF sources and immutable revisions "
        "remain in local PostgreSQL; hosted PDF intake is disabled. Both arms "
        "use the same explicit confirmation and installed core in-process check.",
        f"Core revision: `{report['core']['revision']}`. "
        "Selected Airflow proof is separate from full release readiness (#12).",
        "Local replay is not fresh live integration, publication, hosted "
        "acceptance or learner mastery. Fault injection, when declared, is "
        "labeled separately from actual provider results.",
        "",
        "Paired outcomes, field/state/failure breakdowns, workflow ratios, "
        "timings, hashes, source and run provenance:",
        "",
        "```json",
        json.dumps(report, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def run(manifest_path: Path, plan_path: Path, workdir: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from invoice_triage.app import create_app
    from invoice_triage.db import setup_database
    from invoice_triage.local_pg import LocalPostgres, find_free_port
    from invoice_triage.settings import Settings

    manifest_bytes = read_artifact(manifest_path.parent, manifest_path.name)
    manifest, artifacts, run_evidence = load_bundle(manifest_path)
    plan_bytes = read_artifact(plan_path.parent, plan_path.name)
    plan = Plan.model_validate(load_json(plan_bytes))
    if (
        sha(manifest_bytes) != plan.manifest_sha256
        or code_identity() != plan.code_sha256
    ):
        raise ValueError("manifest or workflow code differs from frozen plan")
    if (manifest.run is None and plan.evidence_class == "first-outputs") or (
        manifest.run is not None and plan.evidence_class != "first-outputs"
    ):
        raise ValueError("workflow evidence class differs from saved-output provenance")
    ids, arms = {d.id for d in manifest.documents}, set(manifest.arms)
    if set(plan.expected) != ids or not set(plan.corrections) <= ids:
        raise ValueError("workflow plan must cover exactly the assigned documents")
    if any(o.document not in ids or o.arm not in arms for o in plan.observations):
        raise ValueError("observation lies outside the assigned comparison")
    references = {
        name
        for c in plan.corrections.values()
        for name in (c.pdf, c.gold, *c.arms.values())
    }
    if references != set(plan.hashes) or any(
        set(c.arms) != arms for c in plan.corrections.values()
    ):
        raise ValueError("correction hash inventory and arms must match exactly")
    extras = {name: read_artifact(plan_path.parent, name) for name in plan.hashes}
    if any(
        sha(data) != manifest.hashes[name] for name, data in artifacts.items()
    ) or any(sha(data) != plan.hashes[name] for name, data in extras.items()):
        raise ValueError("saved artifact hash mismatch")
    core = core_identity(PINNED_CORE)
    workdir.mkdir(parents=False, exist_ok=False)
    pg = LocalPostgres(workdir / "data", find_free_port())
    rows = []
    old_env: dict[str, str | None] | None = None
    try:
        pg.start()
        old_env = configure_governance(traces_dir=workdir / "traces")
        for index, (arm, name) in enumerate(manifest.arms.items()):
            outputs = load_json(artifacts[name])
            for number, doc in enumerate(manifest.documents):
                database = f"replay_{index}_{number}"
                pg.ensure_database(database)
                admin = pg.dsn(db=database)
                creds = provision_governance_schema(admin)
                intake = setup_database(admin)
                configure_governance(audit_dsn=creds.audit_dsn)
                settings = Settings(
                    _env_file=None,
                    intake_dsn=intake.intake_dsn,
                    read_dsn=creds.read_dsn,
                    audit_dsn=creds.audit_dsn,
                    hosted=False,
                )  # type: ignore[call-arg]
                correction = plan.corrections.get(doc.id)
                replacement = (
                    (extras[correction.pdf], load_json(extras[correction.arms[arm]]))
                    if correction
                    else None
                )
                with TestClient(create_app(settings)) as client:
                    kind = (
                        "offline-stub"
                        if run_evidence is None
                        else "controlled-transport"
                        if run_evidence["kind"] == "controlled-transport"
                        else "saved-provider-output"
                    )
                    observed = execute(
                        client,
                        artifacts[doc.pdf],
                        outputs[doc.id],
                        arm=arm,
                        run_id=sha(plan_bytes),
                        kind=kind,
                        correction=replacement,
                    )
                rows.append({"document": doc.id, "arm": arm, **observed})
        # All actions finish before gold is parsed or scoring is invoked.
        if (
            read_artifact(manifest_path.parent, manifest_path.name) != manifest_bytes
            or read_artifact(plan_path.parent, plan_path.name) != plan_bytes
            or code_identity() != plan.code_sha256
        ):
            raise ValueError("analysis inputs or code changed during workflow replay")
        raw = replay(manifest_path)
        gold = load_json(artifacts[manifest.gold])
        for row in rows:
            correction = plan.corrections.get(row["document"])
            expected_fields = gold[row["document"]]["fields"]
            if correction is not None:
                from invoice_triage.pdf_text import PDFText, read_pdf

                converted = read_pdf(extras[correction.pdf])
                parsed = validate_result(
                    load_json(extras[correction.gold]),
                    converted.pages if isinstance(converted, PDFText) else [],
                )
                if isinstance(parsed, Failure):
                    raise ValueError("correction gold must declare all five states")
                expected_fields = parsed.fields.model_dump(mode="json")
            row["expected"] = plan.expected[row["document"]]
            row["fields_agree"] = agrees(row["fields"], expected_fields)
            row["field_disagreements"] = [
                name
                for name, field in expected_fields.items()
                if row["fields"] is None
                or any(
                    row["fields"][name][key] != field[key] for key in ("state", "value")
                )
            ]
            row["correct"] = (
                row["accepted"]
                and row["fields_agree"]
                and row["outcome"] == row["expected"]
            )
            disagreements = ", ".join(row["field_disagreements"]) or "none"
            row["explanation"] = (
                "Expected disposition and all five fields agree."
                if row["correct"]
                else "Refused before intake; no case manufactured."
                if row["outcome"] == "refused"
                else f"Field disagreements: {disagreements}; "
                f"actual {row['outcome']}, expected {row['expected']}. "
                + "; ".join(
                    f"Resolution refused: {event['reason']}"
                    for event in row["events"]
                    if event["action"] == "resolve" and not event["resolved"]
                )
            )
        return {
            "version": "1",
            "live_comparison": (
                "retained user-verified first outputs; not fresh integration"
                if run_evidence and run_evidence["kind"] == "user-verified"
                else "unavailable"
            ),
            "evidence_class": plan.evidence_class,
            "raw": raw,
            "plan_sha256": sha(plan_bytes),
            "workflow_code_sha256": code_identity(),
            "core": core,
            "policy": plan.policy,
            "rows": rows,
            "workflow": summarize(rows, list(manifest.arms)),
            "operator_observations": [o.model_dump() for o in plan.observations],
            "extraction_seconds": None,
            "actual_cost_usd": None,
        }
    finally:
        pg.stop()
        if old_env is not None:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.with_suffix(".md").exists():
            raise ValueError("report already exists; retain it and use a new output")
        report = run(
            args.manifest.resolve(), args.plan.resolve(), args.workdir.resolve()
        )
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        args.output.with_suffix(".md").write_text(render(report))
    except Exception as exc:
        # Database/transport exceptions may contain DSNs or document values.
        print(f"workflow replay refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"Saved JSON and Markdown report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
