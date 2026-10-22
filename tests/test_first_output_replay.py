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


def test_ten_first_outputs_replay_without_credentials_and_preserve_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
    altered = json.loads((root / "model.json").read_text())
    altered["opaque-7"] = gold["opaque-7"]
    (root / "model.json").write_text(json.dumps(altered))
    manifest["hashes"]["model.json"] = hashlib.sha256(
        (root / "model.json").read_bytes()
    ).hexdigest()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="first result"):
        evaluation.replay(path)
