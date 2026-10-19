"""Registry MCP bridge unit tests.

Core assertion (iron rule 3): every ledger operation goes through the
jd_registry_ops.py subprocess with arguments passed as an argv list,
shell=False — so CJK text and spaces are not truncated by the shell. This
test mocks subprocess and never touches the real ledger.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import apa.mcp_registry as br


@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    """subprocess.run mock that captures argv (the mock keeps its own records;
    do not assign call_args manually).

    The ops script is explicitly pointed at a tmp file — **no reliance on any
    private machine layout** (the old version implicitly depended on
    <private-workspace> existing, which would fake-green on public-repo CI).
    """
    ops = tmp_path / "jd_registry_ops.py"
    ops.write_text("# synthetic ops script for tests\n", encoding="utf-8")
    monkeypatch.setenv("APA_REGISTRY_OPS", str(ops))
    with patch.object(br.subprocess, "run") as m:
        m.return_value = subprocess.CompletedProcess([], 0, stdout="OPS OK\n", stderr="")
        yield m


def _last_argv(m) -> list[str]:
    args, _ = m.call_args
    return list(args[0])


def _last_kwargs(m) -> dict:
    _, kwargs = m.call_args
    return kwargs


# ---- iron rule 3: argument passing structure ----

def test_update_uses_argv_list_not_shell(fake_run) -> None:
    """update: Chinese field=value pairs must travel as an argv list,
    shell=False."""
    out = br.registry_update("GOV-SYNTH00002", ["备注=D6 演练：经 MCP 桥更新"], note="通道验证")
    argv = _last_argv(fake_run)
    assert argv[-7:] == [
        "update", "--id", "GOV-SYNTH00002",
        "--set", "备注=D6 演练：经 MCP 桥更新",
        "--note", "通道验证",
    ]
    assert _last_kwargs(fake_run)["shell"] is False
    assert "OPS OK" in out


def test_update_multiple_sets_map_to_repeated_set_flag(fake_run) -> None:
    """Multiple set items map to repeated --set flags one by one (the ops
    action="append" semantics)."""
    br.registry_update("MCF-1", ["裁决=已投", "母本_批次=P0-1已完成", "备注=含空格 与=号"])
    argv = _last_argv(fake_run)
    assert argv.count("--set") == 3
    assert "备注=含空格 与=号" in argv  # spaces and equals signs inside values are not truncated (list passing)


def test_update_rejects_malformed_set_without_calling_ops(fake_run) -> None:
    """A set item missing = is rejected inside the bridge without calling ops
    (fail-fast, avoiding half-baked writes)."""
    out = br.registry_update("GOV-1", ["裁决已投"])
    assert "ERROR" in out
    fake_run.assert_not_called()


def test_query_builds_optional_flags(fake_run) -> None:
    br.registry_query(verdict="可投", contains="UNIVERS")
    argv = _last_argv(fake_run)
    assert argv[1].endswith("jd_registry_ops.py")  # argv[0]=python, argv[1]=ops script
    assert "query" in argv and "--verdict" in argv and "--contains" in argv
    assert _last_kwargs(fake_run)["shell"] is False


def test_add_maps_all_fields(fake_run) -> None:
    br.registry_add(
        id="MCF-999", title="AI Engineer", url="https://x", source="MCF",
        employer="Test Co", deadline="2026-10-20", salary="$6,000-9,000",
        emp_type="Full-time", score=4.2, tier="A", verdict="可投",
        batch="P1", ref="docs/117", archive="jd_archive/x.txt", note="桥测",
    )
    argv = _last_argv(fake_run)
    assert argv[2:5] == ["add", "--id", "MCF-999"]  # argv[0]=python, argv[1]=ops
    assert "--score" in argv and argv[argv.index("--score") + 1] == "4.2"
    assert argv[argv.index("--deadline") + 1] == "2026-10-20"
    assert "--emp-type" in argv  # dest mapping is the ops script's responsibility


def test_delete_is_high_risk_and_passes_reason(fake_run) -> None:
    br.registry_delete("GOV-X", reason="JD 撤下（用户指令）")
    argv = _last_argv(fake_run)
    assert argv[argv.index("delete") + 1 : argv.index("delete") + 3] == ["--id", "GOV-X"]
    assert "JD 撤下（用户指令）" in argv


def test_log_and_addcol(fake_run) -> None:
    br.registry_log("D6 MCP 桥验收")
    assert "log" in _last_argv(fake_run) and "D6 MCP 桥验收" in _last_argv(fake_run)
    br.registry_addcol("APA 状态", default="", note="D6")
    argv = _last_argv(fake_run)
    assert argv[argv.index("addcol") + 1 : argv.index("addcol") + 3] == ["--name", "APA 状态"]


# ---- error propagation and boundaries ----

def test_ops_nonzero_exit_surfaces_stderr(fake_run) -> None:
    """A nonzero ops exit code (e.g. "record ID already exists") must be
    returned as-is, never silently swallowed."""
    fake_run.side_effect = None
    fake_run.return_value = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="记录ID 已存在：GOV-1（用 update 修改）"
    )
    out = br.registry_update("GOV-1", ["裁决=已投"])
    assert "记录ID 已存在" in out


def test_missing_ops_script_returns_error_without_calling(fake_run) -> None:
    with patch.object(br.Path, "exists", return_value=False):
        out = br.registry_stats()
    assert "not found" in out
    fake_run.assert_not_called()


def test_env_overrides_ops_and_python(monkeypatch, fake_run) -> None:
    """The APA_REGISTRY_OPS / APA_REGISTRY_PYTHON overrides take effect (the
    sandbox rehearsal relies on them)."""
    monkeypatch.setenv("APA_REGISTRY_OPS", r"C:\tmp\sandbox\jd_registry_ops.py")
    monkeypatch.setenv("APA_REGISTRY_PYTHON", r"C:\tmp\sandbox\python.exe")
    with patch.object(br.Path, "exists", return_value=True):
        br.registry_stats()
    argv = _last_argv(fake_run)
    assert argv[0] == r"C:\tmp\sandbox\python.exe"
    assert argv[1] == r"C:\tmp\sandbox\jd_registry_ops.py"


def test_encoding_utf8_explicit(fake_run) -> None:
    """Windows Chinese output: encoding explicitly utf-8 (no reliance on the
    locale default)."""
    br.registry_stats()
    assert _last_kwargs(fake_run)["encoding"] == "utf-8"


def test_child_env_forces_utf8_stdout(fake_run) -> None:
    """The child env injects PYTHONIOENCODING=utf-8 — Chinese output is
    written as UTF-8 and read as UTF-8 by the parent.

    A pitfall found in testing: conda run happens to inject this variable
    while running python.exe directly does not, creating the illusion of
    "all green under conda run / mojibake when run directly"; passing the env
    explicitly makes both paths consistent.
    """
    br.registry_stats()
    assert _last_kwargs(fake_run)["env"]["PYTHONIOENCODING"] == "utf-8"


def test_timeout_bounded(fake_run) -> None:
    """Resident channel: ops calls have a timeout upper bound (the ledger has
    756 rows; 120s is enough)."""
    br.registry_stats()
    assert _last_kwargs(fake_run)["timeout"] == 120
