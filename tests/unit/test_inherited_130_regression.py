"""Regression: the engine's inherited rule set, run on _130's 10 historical
cover letters, must produce a finding set identical to the live output of the
singapore workspace's `_130_cl_redline_check.py`.

This is the executable form of the acceptance criterion "the _130 10
historical cover letter files' regression pass rate matches the live script":
instead of comparing a vague "pass rate", it directly compares the
(rule code, description, line, hard) tuple sets.
Live baseline (measured 2026-10-17): 23 items to fix — the regression target
is "consistent", not "zero violations".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from apa.compliance.engine import check_files
from apa.compliance.rules import get_rules
from apa.compliance.rules.inherited_130 import HISTORICAL_CLS

SINGAPORE = Path(r"<private-workspace>")
ME = SINGAPORE / "me"
_130_SCRIPT = SINGAPORE / "docs" / "_130_cl_redline_check.py"

pytestmark = pytest.mark.skipif(
    not _130_SCRIPT.is_file(), reason="singapore workspace not present on this machine (CI skip)"
)

_HEADER_RE = re.compile(r"^(\S+) \((\d+) chars, (\d+) lines\)$")
_FINDING_RE = re.compile(r"^  (🔴|⚠️)\s+\[(R\d+)\] (.+?)  L(\d+): …(.+)…$")
_MISSING_REQ_RE = re.compile(r"^  🔴 \[缺失\] \[(R\d+)\] (.+)$")
_MISSING_FILE_RE = re.compile(r"^!! MISSING (.+)$")
_RESULT_RE = re.compile(r"^RESULT: (?:✅ 全部通过|🔴 (\d+) 项需修)$")


def _parse_130(stdout: str) -> dict:
    files: dict[str, dict] = {}
    bad = None
    cur = None
    for line in stdout.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            cur = m.group(1)
            files[cur] = {"findings": [], "missing": False}
            continue
        m = _MISSING_FILE_RE.match(line)
        if m:
            cur = m.group(1)
            files[cur] = {"findings": [], "missing": True}
            continue
        if cur is None:
            continue
        m = _FINDING_RE.match(line)
        if m:
            files[cur]["findings"].append(
                (m.group(2), m.group(3), int(m.group(4)), m.group(1) == "🔴")
            )
            continue
        m = _MISSING_REQ_RE.match(line)
        if m:
            files[cur]["findings"].append((m.group(1), m.group(2), 0, True))
            continue
        m = _RESULT_RE.match(line)
        if m:
            bad = int(m.group(1) or 0)
    return {"files": files, "bad": bad}


def _engine_side(report_dict: dict) -> dict:
    files = {}
    for f in report_dict["files"]:
        name = Path(f["path"]).name
        files[name] = {
            "findings": [(x["code"], x["desc"], x["line"], x["hard"]) for x in f["findings"]],
            "missing": f["missing"],
        }
    return {"files": files, "bad": report_dict["hard_count"]}


def test_inherited_rules_match_live_130():
    paths = [ME / n for n in HISTORICAL_CLS]
    engine = _engine_side(check_files(paths, get_rules("inherited")).to_dict())

    # Encoding discipline: the _130 output contains the 🔴 emoji, and running
    # it directly on Windows (GBK locale) crashes with UnicodeEncodeError
    # (measured: conda run injecting PYTHONIOENCODING=utf-8 passes, running
    # python.exe directly crashes — both paths must behave identically, so
    # pass the env explicitly).
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    live = subprocess.run(
        [sys.executable, str(_130_SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(SINGAPORE), env=env,
    )
    baseline = _parse_130(live.stdout)

    assert set(baseline["files"]) == set(engine["files"]), "file sets differ"
    for name in sorted(baseline["files"]):
        b = sorted(baseline["files"][name]["findings"])
        e = sorted(engine["files"][name]["findings"])
        assert b == e, f"{name} finding sets differ\n_130:   {b}\nengine: {e}"
        assert baseline["files"][name]["missing"] == engine["files"][name]["missing"]
    assert baseline["bad"] == engine["bad"], (
        f"total count differs: _130={baseline['bad']} engine={engine['bad']}"
    )
