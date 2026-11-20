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


def test_documented_request_identity_functions_are_public() -> None:
    from invoice_triage import model_runner

    for name in ("Plan", "Eligibility", "request_payload", "canonical", "digest"):
        assert callable(getattr(model_runner, name, None)), name
