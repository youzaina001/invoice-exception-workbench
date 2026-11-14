"""Exercise pytest's public preflight before test collection."""

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


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
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "private source repository",
        "source repository is private",
        "source repository remains private",
    )
    for rel in ("site/index.html", "site/walkthrough.html", "site/project-status.html"):
        content = (root / rel).read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in content, f"{rel} still contains {phrase!r}"
    status_content = (root / "site/project-status.html").read_text(encoding="utf-8")
    assert "describe its private source and evidence limits" in status_content


def test_pyproject_dependencies_are_bounded_and_locked() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    deps = (
        pyproject["project"]["dependencies"]
        + pyproject["project"]["optional-dependencies"]["dev"]
    )
    for spec in deps:
        assert any(op in spec for op in ("==", ">=", "<")), spec
        assert not (">=" in spec and "<" not in spec), spec
    assert (root / "uv.lock").is_file()


def test_dev_pg_and_docs_exclude_legacy_runtime_keys() -> None:
    root = Path(__file__).resolve().parents[1]
    dev_pg = (root / "scripts/dev_pg.py").read_text(encoding="utf-8")
    for legacy_key in ("WAREHOUSE_DSN=", "APP_HOST=", "APP_PORT="):
        assert legacy_key not in dev_pg
    local_dev = (root / "docs/local-development.md").read_text(encoding="utf-8")
    assert "The generated `.env` may retain these" not in local_dev
