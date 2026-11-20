"""Synthetic archived runner outputs exercise replay; no reserved corpus is read."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_model_runner import frozen_inputs

from invoice_triage.deterministic import extract_fields
from invoice_triage.pdf_text import ADAPTER, PDFText, read_pdf


@pytest.fixture(autouse=True)
def reset_db() -> None:
    """Archive replay requires no database."""


def _build_synthetic_archive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    from invoice_triage import evaluation, model_runner
    from invoice_triage.extraction import RESULT

    plan, receipt, pdfs = frozen_inputs(tmp_path)
    common = tmp_path / "synthetic-common"
    common.mkdir()
    assert (
        model_runner.main(
            ["prepare", "--plan", str(plan), "--receipt", str(receipt)],
            common_dir=common,
        )
        == 0
    )
    root = tmp_path / "bundle"
    root.mkdir()
    docs, gold, baseline, model = [], {}, {}, {}
    for index in range(10):
        slot = f"evaluation-{index}"
        source = pdfs[slot].read_bytes()
        converted = read_pdf(source)
        assert isinstance(converted, PDFText)
        result = extract_fields(converted).model_dump(mode="json")
        raw = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": json.dumps(result)}]},
                }
            ],
            "modelVersion": "controlled-only",
        }

        def transport(request: httpx.Request) -> httpx.Response:
            if index == 8:
                raise httpx.ReadTimeout("synthetic lost response")
            if index == 9:
                raise KeyboardInterrupt
            if index == 7:
                return httpx.Response(500, json={"error": {"code": 500}})
            return httpx.Response(200, json=raw)

        try:
            outcome = model_runner.main(
                ["send", "--slot", slot, "--pdf", str(pdfs[slot])],
                common_dir=common,
                transport=httpx.MockTransport(transport),
            )
            assert outcome == (0 if index < 7 else 2)
        except KeyboardInterrupt:
            assert index == 9
        doc = f"opaque-{index}"
        (root / f"{doc}.pdf").write_bytes(source)
        docs.append(
            {
                "id": doc,
                "scenario": "synthetic-provenance-test",
                "pages": converted.pages,
                "pdf": f"{doc}.pdf",
                "text_sha256": converted.text_sha256,
            }
        )
        gold[doc] = baseline[doc] = result
        state = json.loads((common / model_runner.HOME_NAME / "state.json").read_text())
        attempt = state["attempts"][slot]
        model[doc] = (
            {
                "version": "1",
                "status": "failure",
                "stage": "extract",
                "code": "uncertain_send",
            }
            if attempt["status"] == "reserved"
            else attempt["result"]
        )
    home = common / model_runner.HOME_NAME
    raw_files = {}
    for path in home.glob("*.raw.json"):
        shutil.copyfile(path, root / path.name)
        raw_files[path.name.removesuffix(".raw.json")] = path.name
    shutil.copyfile(home / "state.json", root / "state.json")
    shutil.copyfile(
        common / (model_runner.HOME_NAME + ".seal.json"), root / "seal.json"
    )
    for name, data in (
        ("gold.json", gold),
        ("baseline.json", baseline),
        ("model.json", model),
        ("schema.json", RESULT.json_schema()),
    ):
        (root / name).write_text(json.dumps(data))
    (root / "prompt.txt").write_text(json.loads(plan.read_text())["prompt"])
    manifest: dict[str, Any] = {
        "version": "1",
        "scope": "reserved",
        "kind": "first-outputs",
        "documents": docs,
        "gold": "gold.json",
        "arms": {"deterministic": "baseline.json", "model": "model.json"},
        "assigned_count": 10,
        "provenance": {
            "frozen_at": "2026-10-01T00:00:00+00:00",
            "developers": ["synthetic developer"],
            "curator": "synthetic independent curator",
            "exposure": ["synthetic test fixture, no real reserved data"],
            "reserved_status": "established",
        },
        "source_base": "synthetic-test",
        "scorer_sha256": evaluation.scorer_identity(),
        "adapter": ADAPTER,
        "prompt": "prompt.txt",
        "schema_file": "schema.json",
        "run": {
            "state": "state.json",
            "seal": "seal.json",
            "raw_files": raw_files,
            "model_arm": "model",
            "baseline_arm": "deterministic",
            "deterministic_sha256": hashlib.sha256(
                Path(model_runner.__file__).with_name("deterministic.py").read_bytes()
            ).hexdigest(),
            "layout": "synthetic-one-layout",
        },
        "hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()
        },
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest))
    capsys.readouterr()
    return path, manifest, gold


def test_ten_first_outputs_replay_without_credentials_and_preserve_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from invoice_triage import evaluation

    path, _, _ = _build_synthetic_archive(tmp_path, capsys)
    report = evaluation.replay(path)
    capsys.readouterr()
    assert report["arms"]["model"]["primary"] == {"correct": 7, "total": 10}
    assert report["run"]["kind"] == "controlled-transport"
    assert report["run"]["actual_cost_usd"] is None
    assert (
        report["run"]["attempts"]["evaluation-0"]["returned_model"] == "controlled-only"
    )
    assert report["arms"]["model"]["failures"] == {
        "validate/invalid_envelope": 1,
        "extract/transport_failure": 1,
        "extract/uncertain_send": 1,
    }


def test_verify_archived_run_refuses_tampered_reservation_and_model_arm(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from invoice_triage import evaluation, extraction, model_runner

    path, _, gold = _build_synthetic_archive(tmp_path, capsys)
    manifest_model, artifacts, _ = evaluation.load_bundle(path)
    arms = {
        arm: extraction.load_json(artifacts[name])
        for arm, name in manifest_model.arms.items()
    }
    assert manifest_model.run is not None

    for incorrect_total in ("0", "999999"):
        state = extraction.load_json(artifacts[manifest_model.run.state])
        state["planned_reservation_usd"] = incorrect_total
        seal = extraction.load_json(artifacts[manifest_model.run.seal])
        seal["state_sha256"] = extraction.digest(extraction.canonical(state))
        mutated_artifacts = dict(artifacts)
        mutated_artifacts[manifest_model.run.state] = extraction.canonical(state)
        mutated_artifacts[manifest_model.run.seal] = extraction.canonical(seal)
        with pytest.raises(ValueError, match="reservation total"):
            model_runner.verify_archived_run(manifest_model, mutated_artifacts, arms)

    mutated_arms = {
        **arms,
        "model": {**arms["model"], "opaque-7": gold["opaque-7"]},
    }
    with pytest.raises(ValueError, match="first result"):
        model_runner.verify_archived_run(manifest_model, artifacts, mutated_arms)


@pytest.mark.parametrize(
    "raw_payload",
    [
        b"[]",
        json.dumps(
            {"candidates": [{"finishReason": "STOP", "content": None}]}
        ).encode(),
        json.dumps(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [None]}}]}
        ).encode(),
        json.dumps(
            {"candidates": [{"finishReason": "STOP", "content": {"parts": [["text"]]}}]}
        ).encode(),
    ],
    ids=["non-dict-root", "null-content", "null-part", "list-part"],
)
def test_verify_archived_run_handles_malformed_raw_gemini_envelopes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw_payload: bytes
) -> None:
    from invoice_triage import evaluation, extraction, model_runner

    path, _, gold = _build_synthetic_archive(tmp_path, capsys)
    manifest_model, artifacts, _ = evaluation.load_bundle(path)
    arms = {
        arm: extraction.load_json(artifacts[name])
        for arm, name in manifest_model.arms.items()
    }
    assert manifest_model.run is not None
    raw_name = manifest_model.run.raw_files["evaluation-7"]

    state = extraction.load_json(artifacts[manifest_model.run.state])
    state["attempts"]["evaluation-7"]["raw_sha256"] = extraction.digest(raw_payload)
    state["attempts"]["evaluation-7"]["returned_model"] = None
    state["attempts"]["evaluation-7"]["usage"] = None
    seal = extraction.load_json(artifacts[manifest_model.run.seal])
    seal["state_sha256"] = extraction.digest(extraction.canonical(state))

    mutated_artifacts = dict(artifacts)
    mutated_artifacts[raw_name] = raw_payload
    mutated_artifacts[manifest_model.run.state] = extraction.canonical(state)
    mutated_artifacts[manifest_model.run.seal] = extraction.canonical(seal)

    evidence = model_runner.verify_archived_run(manifest_model, mutated_artifacts, arms)
    assert evidence["attempts"]["evaluation-7"]["status"] == "failure"

    state["attempts"]["evaluation-7"]["result"] = gold["opaque-7"]
    seal["state_sha256"] = extraction.digest(extraction.canonical(state))
    mutated_artifacts[manifest_model.run.state] = extraction.canonical(state)
    mutated_artifacts[manifest_model.run.seal] = extraction.canonical(seal)
    mutated_arms = {
        **arms,
        "model": {**arms["model"], "opaque-7": gold["opaque-7"]},
    }
    with pytest.raises(ValueError, match="first raw response"):
        model_runner.verify_archived_run(
            manifest_model, mutated_artifacts, mutated_arms
        )


def test_extraction_json_and_artifact_primitives(tmp_path: Path) -> None:
    from invoice_triage import extraction

    assert extraction.load_json(b'{"a": 1, "b": [2]}') == {"a": 1, "b": [2]}
    with pytest.raises(ValueError, match="duplicate JSON key"):
        extraction.load_json(b'{"a": 1, "a": 2}')
    with pytest.raises(ValueError, match="invalid JSON constant"):
        extraction.load_json(b'{"a": NaN}')

    assert extraction.canonical({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    with pytest.raises(ValueError):
        extraction.canonical({"a": float("nan")})
    assert extraction.digest(b"abc") == hashlib.sha256(b"abc").hexdigest()

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "ok.json").write_bytes(b"{}")
    assert extraction.read_artifact(bundle, "ok.json") == b"{}"

    (bundle / ".env").write_bytes(b"SECRET=1")
    with pytest.raises(ValueError, match="environment and key files"):
        extraction.read_artifact(bundle, ".env")

    (bundle / "secret.pem").write_bytes(b"KEY")
    with pytest.raises(ValueError, match="environment and key files"):
        extraction.read_artifact(bundle, "secret.pem")

    (tmp_path / "outside.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="leaves its bundle"):
        extraction.read_artifact(bundle, "../outside.json")

    (bundle / "huge.bin").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds 8 MiB"):
        extraction.read_artifact(bundle, "huge.bin")
