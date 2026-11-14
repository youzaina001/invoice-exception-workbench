"""Exercise pytest's public preflight before test collection."""

import ast
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = ROOT / "site/evidence/saved-report.json"
REPORT_HTML = ROOT / "site/report.html"
REPLAY_TEST = ROOT / "tests/test_workflow_replay.py"


@pytest.fixture(autouse=True)
def reset_db() -> None:
    """These subprocess checks do not use a database."""


def test_public_package_name() -> None:
    assert importlib.util.find_spec("invoice_triage") is not None
    assert importlib.metadata.metadata("invoice-triage")["Name"] == "invoice-triage"


def run_checkout(
    tmp_path: Path,
    *,
    wrong_source: bool = False,
    options: tuple[str, ...] = (),
    wheel_core: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    checkout = tmp_path / "current checkout"
    tests = checkout / "tests"
    tests.mkdir(parents=True)
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests / "conftest.py")
    source = (tmp_path / "old checkout" if wrong_source else checkout) / "src"
    core_source = checkout / ".venv/lib/site-packages" if wheel_core else source
    for package in (source / "invoice_triage", core_source / "airflow_dq_agent"):
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
    if wheel_core:
        # Disposable fixture only; the test must not require signing credentials.
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "Workbench fixture",
            ],
            check=True,
        )
    marker = checkout / "collected"
    (tests / "test_sentinel.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def test_ok(): pass\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(source), str(core_source))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q", *options],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    ), marker


def test_another_checkouts_package_is_rejected_before_collection(
    tmp_path: Path,
) -> None:
    result, marker = run_checkout(tmp_path, wrong_source=True)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "Wrong checkout" in result.stdout + result.stderr
    assert not marker.exists()


def test_current_checkout_with_spaces_reports_both_sources(tmp_path: Path) -> None:
    result, marker = run_checkout(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "current checkout/src/invoice_triage/__init__.py" in result.stdout
    assert "current checkout/src/airflow_dq_agent/__init__.py" in result.stdout
    assert "core_revision=unavailable" in result.stdout
    assert sys.executable in result.stdout


def test_installed_package_testing_is_explicit(tmp_path: Path) -> None:
    result, marker = run_checkout(
        tmp_path, wrong_source=True, options=("--installed-package",)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "Verification (installed-package)" in result.stdout


def test_selected_core_is_rejected_before_collection_when_source_differs(
    tmp_path: Path,
) -> None:
    result, marker = run_checkout(
        tmp_path, options=("--core-checkout", str(tmp_path / "selected core"))
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "Wrong core checkout" in result.stdout + result.stderr
    assert not marker.exists()


def test_explicit_core_checkout_with_spaces_is_accepted(tmp_path: Path) -> None:
    result, marker = run_checkout(
        tmp_path, options=("--core-checkout", str(tmp_path / "current checkout"))
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "current checkout/src/airflow_dq_agent/__init__.py" in result.stdout


def test_wheel_core_does_not_claim_the_workbenchs_git_revision(tmp_path: Path) -> None:
    result, marker = run_checkout(tmp_path, wheel_core=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    assert "core_revision=unavailable" in result.stdout


def test_static_walkthrough_does_not_claim_source_repository_is_private() -> None:
    forbidden = (
        "private source repository",
        "source repository is private",
        "source repository remains private",
    )
    for rel in ("site/index.html", "site/walkthrough.html", "site/project-status.html"):
        content = (ROOT / rel).read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in content, f"{rel} still contains {phrase!r}"
    status_content = (ROOT / "site/project-status.html").read_text(encoding="utf-8")
    assert "describe its private source and evidence limits" in status_content


def test_pyproject_dependencies_are_bounded_and_locked() -> None:
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = (
        pyproject["project"]["dependencies"]
        + pyproject["project"]["optional-dependencies"]["dev"]
    )
    for spec in deps:
        assert any(op in spec for op in ("==", ">=", "<")), spec
        assert not (">=" in spec and "<" not in spec), spec
    assert (ROOT / "uv.lock").is_file()


def test_dev_pg_and_docs_exclude_legacy_runtime_keys() -> None:
    dev_pg = (ROOT / "scripts/dev_pg.py").read_text(encoding="utf-8")
    for legacy_key in ("WAREHOUSE_DSN=", "APP_HOST=", "APP_PORT="):
        assert legacy_key not in dev_pg
    local_dev = (ROOT / "docs/local-development.md").read_text(encoding="utf-8")
    assert "The generated `.env` may retain these" not in local_dev


def test_pinned_core_and_static_report_match() -> None:
    import hashlib
    import re

    from invoice_triage.workflow_replay import PINNED_CORE, Plan, code_identity

    assert PINNED_CORE == "5d602428a93e5eee733b4ed1ffd06af4cbba250b"

    workflow_yml = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(rf"^\s*ref:\s*{PINNED_CORE}\s*$", workflow_yml, re.MULTILINE)

    manifest_path = ROOT / "fixtures/extraction/development/manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    plan_path = ROOT / "fixtures/extraction/development/workflow.json"
    plan_bytes = plan_path.read_bytes()
    plan = Plan.model_validate(json.loads(plan_bytes))
    assert plan.core_revision == PINNED_CORE
    assert plan.code_sha256 == code_identity()
    assert plan.manifest_sha256 == hashlib.sha256(manifest_bytes).hexdigest()

    report_bytes = REPORT_JSON.read_bytes()
    report = json.loads(report_bytes)
    assert report["core"]["revision"] == PINNED_CORE
    assert report["workflow_code_sha256"] == code_identity()
    assert report["plan_sha256"] == hashlib.sha256(plan_bytes).hexdigest()

    report_sha = hashlib.sha256(report_bytes).hexdigest()
    report_html = REPORT_HTML.read_text(encoding="utf-8")
    assert f"Report SHA-256: <code>{report_sha}</code>" in report_html
    assert f"Core: <code>{PINNED_CORE}</code>" in report_html

    status_html = (ROOT / "site/project-status.html").read_text(encoding="utf-8")
    assert f"<code>{PINNED_CORE}</code>" in status_html
    for doc_rel in ("docs/local-development.md", "docs/COMPARISON.md"):
        doc_text = (ROOT / doc_rel).read_text(encoding="utf-8")
        assert PINNED_CORE in doc_text
        assert "a1477241fa856df64687e998347e3ecca315619a" not in doc_text


class _PreBlockParser(HTMLParser):
    """Collect `<pre aria-label="...">...</pre>` text blocks without duplicates."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: dict[str, str] = {}
        self._active_label: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "pre":
            return
        if self._active_label is not None:
            raise AssertionError("Nested <pre> element is not supported")
        label = dict(attrs).get("aria-label")
        if not label:
            raise AssertionError("<pre> element is missing a non-empty aria-label")
        if label in self.blocks:
            raise AssertionError(f"Duplicate <pre> aria-label: {label!r}")
        self._active_label = label
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._active_label is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._active_label is not None:
            self.blocks[self._active_label] = "".join(self._chunks)
            self._active_label = None
            self._chunks = []

    def close(self) -> None:
        super().close()
        if self._active_label is not None:
            raise AssertionError(f"Unclosed <pre> element for {self._active_label!r}")


def _extract_labeled_pre_blocks(html_text: str) -> dict[str, str]:
    parser = _PreBlockParser()
    parser.feed(html_text)
    parser.close()
    return parser.blocks


def test_report_html_embedded_json_matches_saved_report() -> None:
    report_data = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    pre_blocks = _extract_labeled_pre_blocks(REPORT_HTML.read_text(encoding="utf-8"))
    raw_label = "Raw extraction breakdown"
    disp_label = "Disposition groups"
    assert set(pre_blocks) == {raw_label, disp_label}

    raw_embedded = json.loads(pre_blocks[raw_label])
    assert raw_embedded == {key: report_data["raw"][key] for key in ("arms", "paired")}

    disp_embedded = json.loads(pre_blocks[disp_label])
    assert disp_embedded == {
        arm: stats["by_expected"] for arm, stats in report_data["workflow"].items()
    }


def test_replay_tests_import_pinned_core_from_workflow_replay() -> None:
    from invoice_triage.workflow_replay import PINNED_CORE

    replay_test_text = REPLAY_TEST.read_text(encoding="utf-8")
    tree = ast.parse(replay_test_text, filename=str(REPLAY_TEST))

    pinned_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "invoice_triage.workflow_replay"
        and any(
            alias.name == "PINNED_CORE" and alias.asname is None for alias in node.names
        )
    ]
    assert pinned_imports, (
        "tests/test_workflow_replay.py must import PINNED_CORE from "
        "invoice_triage.workflow_replay"
    )

    redefined = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "PINNED_CORE"
                for target in node.targets
            )
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PINNED_CORE"
        )
    ]
    assert not redefined, "tests/test_workflow_replay.py must not shadow PINNED_CORE"
    assert any(
        isinstance(node, ast.Name) and node.id == "PINNED_CORE"
        for node in ast.walk(tree)
    )
    assert PINNED_CORE not in replay_test_text
