"""Acceptance: runtime environment (conda env apa / Python 3.11 / CLI entry / self-check pytest).

Design principle: when these tests run inside env apa (via the wrapper or CI,
both use conda run -n apa python -m pytest), "the current interpreter is the
environment under test" — no subprocess self-proof needed. The existence of the
conda env is the only host-machine probe (CI without conda skips automatically,
see skipif).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONDA_EXE = os.environ.get("APA_CONDA", r"E:\Users\Administrator\miniconda3\Scripts\conda.exe")

needs_conda = pytest.mark.skipif(
    not Path(CONDA_EXE).is_file(), reason=f"no conda on host ({CONDA_EXE}) — skipped in CI environments"
)


def test_running_interpreter_is_python311():
    assert sys.version_info[:2] == (3, 11), f"current interpreter {sys.version}, expected 3.11"


def test_apa_package_installed_and_versioned():
    import apa

    assert apa.__version__ == "0.1.0"


def test_cli_entry_runs(capsys):
    from apa.cli import main

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "W1 complete" in out  # milestone marker, updated as work progresses


@needs_conda
def test_conda_env_apa_exists():
    listing = subprocess.run(
        [CONDA_EXE, "env", "list"], capture_output=True, text=True, check=True
    ).stdout
    assert re_search_env(listing), "apa not found in conda env list"


def re_search_env(listing: str) -> bool:
    import re

    return bool(re.search(r"(?m)^apa\s", listing))


@needs_conda
def test_sqlite_runtime_paths_configured():
    """Runtime paths: SQLite points at data/ inside the repo (gitignored); the model
    directory is env-configurable (public-release semantics: APA_MODELS_DIR takes
    precedence, defaulting to models/ inside the repo)."""
    import os

    from apa import config

    assert config.SQLITE_PATH.parent == REPO / "data"
    expected_models = os.environ.get("APA_MODELS_DIR") or (REPO / "models")
    assert config.DEV_MODELS_DIR == Path(expected_models)
    assert shutil.which("git") is not None
