"""Acceptance: complete one ledger update through the MCP bridge (sandbox copy,
never touches the real ledger).

Per the acceptance criteria: "complete one ledger update via MCP (update the
note of a to-be-reviewed posting)". This test rehearses the full code path on
a **copy** of the singapore assets (bridge → ops subprocess → xlsx persistence
→ operation log) with zero changes to the real ledger; a separate read-only
connectivity test hits the real ledger directly (stats writes nothing).

Iron-rule discipline:
- the ops script and the xlsx must live in the same directory (ops locates
  itself via __file__) → the sandbox is a temp directory holding copies of both
- python explicitly uses the current interpreter (the apa env has openpyxl;
  the production default is conda base — see the bridge docstring)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

import apa.mcp_registry as br

SINGAPORE_DOCS = Path(
    __import__("os").environ.get("APA_SINGAPORE_ROOT", r"<private-workspace>")) / "docs"
OPS_SRC = SINGAPORE_DOCS / "jd_registry_ops.py"
XLSX_SRC = SINGAPORE_DOCS / "JD总台账.xlsx"

pytestmark = pytest.mark.skipif(
    not (OPS_SRC.exists() and XLSX_SRC.exists()),
    reason="singapore assets not present on this machine (repo stays CI-runnable without them)",
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Sandbox: copies of the ops script + ledger; the bridge's env points at
    the sandbox and the current interpreter."""
    shutil.copy(OPS_SRC, tmp_path / "jd_registry_ops.py")
    xlsx_copy = tmp_path / "ledger.xlsx"
    shutil.copy(XLSX_SRC, xlsx_copy)
    monkeypatch.setenv("APA_REGISTRY_OPS", str(tmp_path / "jd_registry_ops.py"))
    monkeypatch.setenv("APA_REGISTRY_PYTHON", sys.executable)
    return {"dir": tmp_path, "xlsx": xlsx_copy}


def _read_row(xlsx: Path, job_id: str) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx)
    ws = wb["总台账"]
    hmap = {ws.cell(row=1, column=c).value: c for c in range(1, ws.max_column + 1)}
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(row=r, column=hmap["记录ID"]).value) == job_id:
            return {h: ws.cell(row=r, column=c).value for h, c in hmap.items()}
    raise AssertionError(f"{job_id} not found in sandbox ledger")


def _log_tail(xlsx: Path, n: int = 3) -> list[tuple]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx)
    ws3 = wb["操作日志"]
    rows = []
    for r in range(max(2, ws3.max_row - n + 1), ws3.max_row + 1):
        rows.append(tuple(ws3.cell(row=r, column=c).value for c in range(1, 9)))
    return rows


def test_sandbox_update_via_bridge_end_to_end(sandbox) -> None:
    """Acceptance main path: update a to-be-reviewed posting's note via the MCP
    bridge → persisted + logged."""
    import openpyxl

    wb = openpyxl.load_workbook(sandbox["xlsx"])
    ws = wb["总台账"]
    hmap = {ws.cell(row=1, column=c).value: c for c in range(1, ws.max_column + 1)}
    target = None
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(row=r, column=hmap["裁决"]).value) == "待核":
            target = str(ws.cell(row=r, column=hmap["记录ID"]).value)
            break
    assert target, "sandbox ledger has no 待核 row to rehearse on"

    note = "D6 MCP bridge sandbox rehearsal (real ledger untouched)"
    out = br.registry_update(target, [f"备注={note}"], note="D6 acceptance")
    assert "UPDATE" in out, out
    assert f"备注" in out

    # persistence check: the note column is updated + the last-updated column refreshed

    row = _read_row(sandbox["xlsx"], target)
    assert row["备注"] == note
    assert str(row["最后更新"]) != ""

    # operation-log check: the last row is this update
    tail = _log_tail(sandbox["xlsx"], 1)[0]
    assert tail[1] == "update" and tail[2] == target and tail[3] == "备注"


def test_sandbox_query_via_bridge(sandbox) -> None:
    """Reading back the sandbox ledger via the bridge's query (read-only path
    connected)."""
    out = br.registry_query(verdict="待核")
    assert "hit(s)" in out


def test_sandbox_ops_error_propagates(sandbox) -> None:
    """An ops rejection (unknown ID) is returned faithfully through the
    bridge — never silent."""
    out = br.registry_update("NO-SUCH-ID-000", ["备注=x"])
    assert "未找到记录ID" in out


def test_real_ledger_readonly_connectivity(monkeypatch) -> None:
    """Read-only connectivity: run stats against the **real** ledger through
    the bridge (no persistence, iron-rule python default)."""
    monkeypatch.setenv("APA_REGISTRY_PYTHON", sys.executable)
    out = br.registry_stats()
    assert "总计" in out, out
    assert "按裁决" in out


def test_sandbox_add_then_query_roundtrip(sandbox) -> None:
    """add → query roundtrip (sandbox): add one synthetic record and read it
    back."""
    out = br.registry_add(
        id="MCF-SANDBOX-0001",
        title="Sandbox Rehearsal Posting (not a real job)",
        url="https://example.invalid/sandbox",
        verdict="待核",
        note="D6 sandbox add rehearsal",
    )
    assert "ADD OK" in out, out
    out2 = br.registry_query(job_id="MCF-SANDBOX-0001")
    assert "Sandbox Rehearsal Posting" in out2
    assert "1 hit(s)" in out2
