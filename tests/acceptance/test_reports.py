"""Acceptance: existence and key facts of the three reports (reports/).

Status bits are designed to evolve: INC-2026-1016 should move from OPEN to
CLOSED once the 2026-10-19 08:00 maintenance window closes and the backfill is
written back — the test accepts either legal status but requires each to be
structurally complete.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INCIDENTS = REPO / "reports/incidents"
WEEKLY = REPO / "reports/weekly"

# Incident reports contain real application data and are NOT shipped in the
# public repo (exposure discipline) — skip gracefully when absent.
pytestmark = pytest.mark.skipif(
    not (INCIDENTS / "INC-2026-1015.md").exists(),
    reason="incident reports not present (public repo: sanitized versions pending)",
)


def _status_of(path: Path) -> str:
    m = re.search(r"\|\s*状态\s*\|\s*\*{0,2}([A-Z]+)\*{0,2}", path.read_text(encoding="utf-8"))
    assert m, f"no status row found in {path.name}"
    return m.group(1)


def test_incident_1015_closed_with_corrections():
    p = INCIDENTS / "INC-2026-1015.md"
    assert p.is_file()
    assert _status_of(p) == "CLOSED"
    text = p.read_text(encoding="utf-8")
    assert "256" in text, "missing the 256-character fact"
    assert "per-form" in text, "missing correction 1 (per-form discovery model)"
    assert "原版简历" in text, "missing correction 2 (original resume attached)"


def test_incident_1016_status_and_window():
    p = INCIDENTS / "INC-2026-1016.md"
    assert p.is_file()
    status = _status_of(p)
    assert status in ("OPEN", "CLOSED"), f"illegal status: {status}"
    text = p.read_text(encoding="utf-8")
    assert "2026-10-19 08:00" in text, "missing maintenance window end"
    if status == "OPEN":
        assert "待办" in text, "OPEN status must have a TODO section"
        assert "补投" in text


def test_w1_baseline_protocol():
    p = WEEKLY / "W1_baseline.md"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    for token in ("TODO", "T1", "T5", "Wilcoxon", "bootstrap"):
        assert token in text, f"W1_baseline.md missing key content: {token}"
