"""Command replay uses real isolated PostgreSQL and the installed core, offline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_evaluation import freeze_bundle, single_document_bundle


@pytest.fixture(autouse=True)
def reset_db() -> None:
    """The command owns its isolated cluster, never the application's database."""


def test_replay_keeps_wrong_confirmed_clearance_and_failures_in_denominator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from invoice_triage.workflow_replay import PINNED_CORE, code_identity, main

    bundle, manifest, doc = single_document_bundle(tmp_path, "S02")
    # Labeled test fault: one saved arm guesses an amount on the missing-amount PDF.
    baseline = bundle / manifest["arms"]["baseline-stub"]
    outputs = json.loads(baseline.read_text())
    outputs[doc]["fields"]["amount_minor"].update(state="value", value=9999)
    baseline.write_text(json.dumps(outputs))
    path = freeze_bundle(bundle, manifest)
    plan = {
        "version": "1",
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "code_sha256": code_identity(),
        "core_revision": PINNED_CORE,
        "policy": "confirm-unchanged-then-declared-source-replacement-v1",
        "expected": {doc: "open"},
        "corrections": {},
        "hashes": {},
        "observations": [],
        "evidence_class": "fault-injection",
    }
    plan_path = bundle / "workflow.json"
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / "report.json"
    assert (
        main(
            [
                str(path),
                "--plan",
                str(plan_path),
                "--workdir",
                str(tmp_path / "run"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    report = json.loads(output.read_text())
    assert report["live_comparison"] == "unavailable"
    assert report["operator_observations"] == []
    assert report["workflow"]["baseline-stub"]["incorrect_no_case"] == {
        "wrong": 1,
        "total": 1,
        "ratio": "1/1",
    }
    assert report["workflow"]["model-stub"]["incorrect_no_case"]["ratio"] == "N/A"
    assert report["workflow"]["model-stub"]["outcomes"] == {"open": 1}
    assert len(report["rows"]) == 2
    assert all(row["confirmation"] == "explicit-scripted" for row in report["rows"])
    assert all(row["review_seconds"] is None for row in report["rows"])
    assert (tmp_path / "run/data/postmaster.pid").exists() is False
    assert "No Operator observations" in output.with_suffix(".md").read_text()


@pytest.mark.parametrize("fault", ["none", "wrong-reference", "wrong-amount"])
def test_declared_replacement_can_only_resolve_the_actual_checked_case(
    tmp_path: Path, fault: str
) -> None:
    from test_evaluation import make_pdf

    from invoice_triage.deterministic import extract_fields
    from invoice_triage.pdf_text import PDFText, read_pdf
    from invoice_triage.workflow_replay import PINNED_CORE, code_identity, main

    bundle, manifest, doc = single_document_bundle(tmp_path, "S02")
    path = freeze_bundle(bundle, manifest)
    reference = "unrelated-invoice" if fault == "wrong-reference" else "Inv-0002"
    source = make_pdf(
        [
            f"Invoice ID: {reference}\nSupplier ID: Sup-0042\n"
            "Invoice date: 2026-10-18\nCurrency: USD\nInvoice total: 125.00\n"
        ]
    )
    (bundle / "corrected.pdf").write_bytes(source)
    pdf = read_pdf(source)
    assert isinstance(pdf, PDFText)
    result = extract_fields(pdf).model_dump(mode="json")
    (bundle / "corrected.json").write_text(json.dumps(result))
    observed = json.loads(json.dumps(result))
    if fault == "wrong-amount":
        observed["fields"]["amount_minor"]["value"] = 9999
    (bundle / "observed.json").write_text(json.dumps(observed))
    plan: dict[str, Any] = {
        "version": "1",
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "code_sha256": code_identity(),
        "core_revision": PINNED_CORE,
        "policy": "confirm-unchanged-then-declared-source-replacement-v1",
        "expected": {doc: "resolved"},
        "corrections": {
            doc: {
                "pdf": "corrected.pdf",
                "arms": {arm: "observed.json" for arm in manifest["arms"]},
                "gold": "corrected.json",
            }
        },
        "hashes": {
            name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
            for name in ("corrected.pdf", "corrected.json", "observed.json")
        },
        "observations": [
            {
                "document": doc,
                "arm": "baseline-stub",
                "active_seconds": 12.5,
                "method": "Synthetic observation test; active time excludes wait.",
            }
        ],
        "evidence_class": "authored-example" if fault == "none" else "fault-injection",
    }
    (bundle / "workflow.json").write_text(json.dumps(plan))
    output = tmp_path / "report.json"
    assert (
        main(
            [
                str(path),
                "--plan",
                str(bundle / "workflow.json"),
                "--workdir",
                str(tmp_path / "run"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads(output.read_text())
    for row in report["rows"]:
        assert row["outcome"] == ("open" if fault == "wrong-reference" else "resolved")
        assert row["correct"] is (fault == "none")
        assert row["events"][2]["action"] == "checks"
        assert row["events"][2]["case_created"] is True
        assert row["review_seconds"] is None
    if fault == "wrong-amount":
        for stats in report["workflow"].values():
            assert stats["incorrect_resolutions"] == {
                "wrong": 1,
                "total": 1,
                "ratio": "1/1",
            }
            assert stats["correctly_cleared"] == 0
            assert stats["cost_per_correctly_cleared"] == "N/A"
    assert (
        "Operator observations are supplied separately"
        in output.with_suffix(".md").read_text()
    )


def test_changed_freeze_refuses_before_creating_a_cluster(tmp_path: Path) -> None:
    from invoice_triage.workflow_replay import main

    bundle, manifest, _ = single_document_bundle(tmp_path)
    path = freeze_bundle(bundle, manifest)
    plan = bundle / "workflow.json"
    plan.write_text("{}")
    assert (
        main(
            [
                str(path),
                "--plan",
                str(plan),
                "--workdir",
                str(tmp_path / "never-created"),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "never-created").exists()
