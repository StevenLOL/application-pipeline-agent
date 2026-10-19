"""Acceptance: repository structure and git discipline (layout and acceptance
criteria per the design docs).

Pure Python + pathlib + subprocess(git), no third-party dependencies — CI
(ubuntu-latest) can run this directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import tomllib
import pytest

REPO = Path(__file__).resolve().parents[2]

MODULES = [
    "cli", "config", "models", "ingest", "registry", "compliance",
    "retrieve", "channel", "adapt", "audit", "tracing", "privacy", "pipeline",
]

DIRS = [
    "evals", "channel_profiles", "prompts", ".github/workflows",
    "reports/incidents", "reports/weekly", "data", "models",
]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def test_pyproject_metadata():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project = meta["project"]
    assert project["name"] == "apa"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["apa"] == "apa.cli:main"


def test_thirteen_module_stubs():
    missing = []
    for m in MODULES:
        f = REPO / "src/apa/compliance/__init__.py" if m == "compliance" else REPO / f"src/apa/{m}.py"
        if not f.is_file():
            missing.append(m)
    assert not missing, f"missing module stubs: {missing}"


def test_compliance_subpackages():
    assert (REPO / "src/apa/compliance/engine.py").is_file()
    assert (REPO / "src/apa/compliance/rules/__init__.py").is_file()


@pytest.mark.parametrize("d", DIRS)
def test_layout_dirs_exist(d):
    assert (REPO / d).is_dir(), f"missing directory: {d}"


def test_gitignore_excludes_pii_dirs():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/" in lines
    assert "models/" in lines


def test_pii_dirs_not_tracked_by_git():
    tracked = _git("ls-files").splitlines()
    leaked = [f for f in tracked if f.startswith(("data/", "models/"))]
    assert not leaked, f"PII directories tracked by git: {leaked}"


def test_git_history_contains_d1():
    log = _git("log", "--oneline")
    count = int(_git("rev-list", "--count", "HEAD").strip())
    assert count >= 2, f"commit count {count} < 2 (expected at least two commits: skeleton + report)"
    assert "D1" in log, "no commit containing D1 found in git history"
