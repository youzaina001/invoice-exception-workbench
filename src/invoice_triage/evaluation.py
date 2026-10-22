"""Offline replay of saved extraction outputs; this command never sends a model call."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from invoice_triage.extraction import (
    RESULT,
    Failure,
    StrictModel,
    Success,
    validate_result,
)
from invoice_triage.pdf_text import ADAPTER, MAX_PDF_BYTES, PDFText, read_pdf


class Document(StrictModel):
    id: str
    scenario: str
    pages: list[str]
    pdf: str
    text_sha256: str


class Provenance(StrictModel):
    frozen_at: str
    developers: list[str]
    curator: str | None
    exposure: list[str]
    reserved_status: Literal["not-established", "established"]


class SavedRun(StrictModel):
    state: str
    seal: str
    raw_files: dict[str, str]
    model_arm: str
    baseline_arm: str
    deterministic_sha256: str
    layout: str


class Manifest(StrictModel):
    version: Literal["1"]
    scope: Literal["development", "reserved"]
    kind: Literal["offline-stub", "first-outputs"]
    documents: list[Document]
    gold: str
    arms: dict[str, str]
    assigned_count: int
    provenance: Provenance
    source_base: str
    scorer_sha256: str
    adapter: dict[str, Any]
    prompt: str
    schema_file: str
    hashes: dict[str, str]
    run: SavedRun | None = None


def scorer_identity() -> str:
    root = Path(__file__).resolve().parent
    return hashlib.sha256(
        b"".join(
            (root / name).read_bytes()
            for name in (
                "evaluation.py",
                "extraction.py",
                "pdf_text.py",
                "comparison_evidence.py",
                "deterministic.py",
            )
        )
    ).hexdigest()


def read_artifact(root: Path, name: str) -> bytes:
    if any(p.startswith(".env") or p.endswith(".pem") for p in Path(name).parts):
        raise ValueError("environment and key files are not replay artifacts")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("artifact path leaves its bundle")
    with path.open("rb") as source:
        data = source.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("artifact exceeds 8 MiB")
    return data


def load_json(data: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(
        data, object_pairs_hook=unique_keys, parse_constant=reject_constant
    )


def load_bundle(path: Path) -> tuple[Manifest, dict[str, bytes], dict[str, Any] | None]:
    """Verify frozen inputs without parsing labels or computing a score."""
    root = path.resolve().parent
    manifest_bytes = read_artifact(root, path.name)
    manifest = Manifest.model_validate(load_json(manifest_bytes))
    if not manifest.documents or len(manifest.arms) != 2:
        raise ValueError("replay needs documents and exactly two saved arms")
    if len(manifest.documents) != manifest.assigned_count:
        raise ValueError("document count differs from the frozen assignment")
    identity = scorer_identity()
    if identity != manifest.scorer_sha256:
        raise ValueError(
            "scorer hash mismatch; changed scoring requires a new analysis"
        )
    if manifest.adapter != ADAPTER:
        raise ValueError("adapter options differ from the frozen analysis")
    references = {
        manifest.gold,
        manifest.prompt,
        manifest.schema_file,
        *manifest.arms.values(),
        *(doc.pdf for doc in manifest.documents),
    }
    if manifest.run is not None:
        references.update(
            (manifest.run.state, manifest.run.seal, *manifest.run.raw_files.values())
        )
    elif manifest.kind != "offline-stub" or manifest.scope != "development":
        raise ValueError(
            "reserved or first-output analysis needs retained runner evidence"
        )
    if set(manifest.hashes) != references:
        raise ValueError("hash inventory must match exactly the referenced artifacts")
    artifacts = {}
    for name, expected_hash in manifest.hashes.items():
        data = read_artifact(root, name)
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError(f"artifact hash mismatch: {name}; create a new analysis")
        artifacts[name] = data
    if load_json(artifacts[manifest.schema_file]) != RESULT.json_schema():
        raise ValueError("saved result schema differs from the scorer contract")
    for doc in manifest.documents:
        converted = read_pdf(artifacts[doc.pdf])
        if not isinstance(converted, PDFText):
            failure_hash = hashlib.sha256(
                json.dumps(
                    converted.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if (
                manifest.run is not None
                and doc.pages == []
                and doc.text_sha256 == failure_hash
            ):
                continue
            raise ValueError(f"PDF refused: {doc.id}/{converted.code}")
        if converted.pages != doc.pages or converted.text_sha256 != doc.text_sha256:
            raise ValueError("canonical text differs from the frozen analysis")
    ids = {doc.id for doc in manifest.documents}
    if len(ids) != len(manifest.documents):
        raise ValueError("duplicate document IDs")
    raw_arms = {arm: load_json(artifacts[name]) for arm, name in manifest.arms.items()}
    if any(set(raw) != ids for raw in raw_arms.values()):
        raise ValueError("both arms must cover exactly the assigned documents")
    run_evidence = None
    if manifest.run is not None:
        from invoice_triage.comparison_evidence import check_run

        run_evidence = check_run(manifest, artifacts, raw_arms)
    return manifest, artifacts, run_evidence


def replay(path: Path) -> dict[str, Any]:
    manifest, artifacts, run_evidence = load_bundle(path)
    manifest_bytes = read_artifact(path.parent, path.name)
    identity = scorer_identity()
    gold_raw = load_json(artifacts[manifest.gold])
    raw_arms = {arm: load_json(artifacts[name]) for arm, name in manifest.arms.items()}
    if set(gold_raw) != {doc.id for doc in manifest.documents}:
        raise ValueError("gold must cover exactly the assigned documents")
    gold: dict[str, Success] = {}
    for doc in manifest.documents:
        parsed = validate_result(gold_raw[doc.id], doc.pages)
        if not isinstance(parsed, Success):
            raise ValueError("gold must declare five field states, not a failure")
        gold[doc.id] = parsed

    arms: dict[str, Any] = {}
    paired: dict[str, dict[str, Any]] = {doc.id: {} for doc in manifest.documents}
    for arm, raw in raw_arms.items():
        correct = 0
        definite: dict[str, dict[str, Any]] = {
            name: {"correct": 0, "total": 0}
            for name in next(iter(gold.values())).fields.items()
        }
        confusion: dict[str, Counter[str]] = {name: Counter() for name in definite}
        false_markings: dict[str, Counter[str]] = {name: Counter() for name in definite}
        failures: Counter[str] = Counter()
        for doc in manifest.documents:
            try:
                result = validate_result(raw[doc.id], doc.pages)
            except ValueError:
                result = Failure(
                    version="1",
                    status="failure",
                    stage="validate",
                    code="invalid_result",
                )
            fields = result.fields.items() if isinstance(result, Success) else {}
            exact = isinstance(result, Success)
            for name, expected in gold[doc.id].fields.items().items():
                observed = fields.get(name)
                state = observed.state if observed else "failure"
                confusion[name][f"{expected.state}->{state}"] += 1
                agrees = observed is not None and (
                    observed.state == expected.state
                    and observed.value == expected.value
                )
                exact = exact and agrees
                if expected.state == "value":
                    definite[name]["total"] += 1
                    definite[name]["correct"] += int(agrees)
                if observed and state != expected.state:
                    false_markings[name][f"false_{state}"] += 1
            if isinstance(result, Failure):
                failures[f"{result.stage}/{result.code}"] += 1
            correct += int(exact)
            paired[doc.id][arm] = {
                "exact": exact,
                "failure": result.model_dump() if isinstance(result, Failure) else None,
            }
        for counts in definite.values():
            counts["accuracy"] = (
                f"{counts['correct']}/{counts['total']}" if counts["total"] else "N/A"
            )
        arms[arm] = {
            "primary": {"correct": correct, "total": len(manifest.documents)},
            "definite_fields": definite,
            "state_confusion": confusion,
            "false_markings": false_markings,
            "failures": failures,
        }
    scores = {stats["primary"]["correct"] for stats in arms.values()}
    return {
        "version": "1",
        "scope": manifest.scope,
        "kind": manifest.kind,
        "analysis_id": hashlib.sha256(manifest_bytes + identity.encode()).hexdigest(),
        "scorer_sha256": identity,
        "provenance": manifest.provenance.model_dump(),
        "source_base": manifest.source_base,
        **({"run": run_evidence} if run_evidence is not None else {}),
        "arms": arms,
        "paired": paired,
        "comparison": (
            "Equal counts: no difference established on this corpus."
            if len(scores) == 1
            else "Counts differ on this corpus; no general superiority is established."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["replay", "text"])
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "replay":
            report = replay(args.path)
        else:
            with args.path.open("rb") as source:
                data = source.read(MAX_PDF_BYTES + 1)
            converted = read_pdf(data)
            print(converted.model_dump_json())
            return 2 if isinstance(converted, Failure) else 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"replay refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
