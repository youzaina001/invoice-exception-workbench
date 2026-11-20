"""The evaluator-facing runner names documented in docs/experiments.md."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "docs/experiments.md"


@pytest.fixture(autouse=True)
def reset_db() -> None:
    """These interface checks do not use a database."""


def test_documented_schema_command_runs() -> None:
    command = re.compile(r"\.venv/bin/python -c '([^']*model_json_schema[^']*)'")
    match = command.search(EXPERIMENTS.read_text())
    assert match, "docs/experiments.md lost the plan/eligibility schema command"
    result = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    schemas = json.loads(result.stdout)
    assert schemas["plan"]["type"] == "object"
    assert schemas["eligibility"]["type"] == "object"


def test_documented_offline_request_identity_matches_frozen_slots(
    tmp_path: Path,
) -> None:
    from test_model_runner import frozen_inputs

    from invoice_triage.extraction import canonical, digest
    from invoice_triage.model_runner import Eligibility, Plan, request_payload
    from invoice_triage.pdf_text import PDFText, read_pdf

    plan_path, receipt_path, pdfs = frozen_inputs(tmp_path)
    plan = Plan.model_validate_json(plan_path.read_text())
    receipt = Eligibility.model_validate_json(receipt_path.read_text())
    for slot in plan.slots:
        pdf = read_pdf(pdfs[slot.id].read_bytes())
        assert isinstance(pdf, PDFText), slot.id
        identity = digest(canonical(request_payload(plan, receipt, pdf.text)))
        assert identity == slot.request_sha256, slot.id
