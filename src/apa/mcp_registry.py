"""Registry MCP bridge (D6): wraps a job-ledger ops script as MCP tools.

Discipline (inherited from the working workspace):
1. **Single channel**: every create/read/update/delete on the JD ledger goes
   through the ops script; this bridge re-implements no business logic (no
   direct openpyxl I/O) — it only orchestrates arguments and spawns the
   subprocess.
2. **No shell strings**: arguments always travel as subprocess argv lists
   (``shell=False``) so CJK text and spaces survive Windows cmd intact.
3. **Python selection**: env-first (``APA_REGISTRY_PYTHON`` / ``APA_PYTHON``),
   falling back to the interpreter running this server (``sys.executable``) —
   no private machine layout is baked in.

Configuration (env):
- ``APA_REGISTRY_OPS``: absolute path to the ops script. Derived from
  ``APA_SINGAPORE_ROOT`` (``<root>/docs/jd_registry_ops.py``) when set;
  unset means the bridge reports a clear error instead of guessing.
- ``APA_REGISTRY_PYTHON``: interpreter for the ops script
  (default: the interpreter running this server).

Start the stdio server: ``python -m apa.mcp_registry``
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("apa-registry")


def _ops_path() -> str:
    explicit = os.environ.get("APA_REGISTRY_OPS")
    if explicit:
        return explicit
    sg = os.environ.get("APA_SINGAPORE_ROOT", "").strip()
    if sg:
        return str(Path(sg) / "docs" / "jd_registry_ops.py")
    return ""                      # unset: fail loud, never guess a private path


def _python() -> str:
    return (os.environ.get("APA_REGISTRY_PYTHON")
            or os.environ.get("APA_PYTHON")
            or sys.executable)


def run_ops(argv: list[str]) -> str:
    """Run one ops command through the ledger script (argv structure).

    Returns merged stdout+stderr; a non-zero ops exit contributes its own
    message (the script's sys.exit text IS the ledger's rejection reason).

    Windows encoding discipline: the child process gets an explicit
    ``PYTHONIOENCODING=utf-8`` so its CJK output round-trips as UTF-8
    regardless of locale (``conda run`` happens to inject it, a direct
    python.exe does not — that asymmetry once produced a "green under
    conda run / mojibake when run directly" false picture).
    """
    ops = _ops_path()
    if not ops:
        return ("ERROR: no ledger ops script configured — set "
                "APA_REGISTRY_OPS (or APA_SINGAPORE_ROOT)")
    if not Path(ops).exists():
        return f"ERROR: ledger ops script not found at {ops} (set APA_REGISTRY_OPS)"
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [_python(), ops, *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,  # never pass arguments as a shell string
        timeout=120,
        env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or f"(no output, exit {proc.returncode})"


@mcp.tool()
def registry_query(
    job_id: Optional[str] = None,
    verdict: Optional[str] = None,
    contains: Optional[str] = None,
) -> str:
    """Query the JD master ledger (read-only). Filterable by record ID / verdict / full-text contains.

    Executed via the jd_registry_ops.py query subcommand (single channel, an
    iron rule of the working workspace).
    """
    argv = ["query"]
    if job_id:
        argv += ["--id", job_id]
    if verdict:
        argv += ["--verdict", verdict]
    if contains:
        argv += ["--contains", contains]
    return run_ops(argv)


@mcp.tool()
def registry_stats() -> str:
    """JD master ledger statistics (read-only): totals / by source / by verdict / scored averages."""
    return run_ops(["stats"])


@mcp.tool()
def registry_update(job_id: str, sets: list[str], note: str = "") -> str:
    """Update one JD record (write operation, via the single channel, auto-recorded in the operation log).

    Args:
        job_id: record ID (e.g. GOV-SYNTH00002 / MCF-1712844)
        sets: list of field=value pairs, each mapped to --set (e.g. ["裁决=已投", "备注=D6 演练"]);
              field names must be ledger column headers (ops rejects unknown fields)
        note: operation note (recorded in the operation log)

    Warning: write operation - modifies JD总台账.xlsx and auto-recomputes the
    summary. Destructive rollback is outside this tool's scope and requires an
    explicit user instruction.
    """
    argv = ["update", "--id", job_id]
    for pair in sets:
        if "=" not in pair:
            return f"ERROR: set item must be field=value, got {pair!r}"
        argv += ["--set", pair]
    if note:
        argv += ["--note", note]
    return run_ops(argv)


@mcp.tool()
def registry_add(
    id: str,
    title: str,
    url: str = "",
    source: str = "MCF",
    employer: str = "",
    deadline: str = "",
    salary: str = "",
    emp_type: str = "",
    score: Optional[float] = None,
    tier: str = "",
    verdict: str = "pending",
    batch: str = "",
    ref: str = "",
    archive: str = "",
    note: str = "",
    http: str = "200",
) -> str:
    """Add a new JD record (write operation, via the single channel).

    ops rejects it when the record ID already exists (use registry_update to modify).
    """
    argv = [
        "add", "--id", id, "--title", title, "--url", url, "--source", source,
        "--employer", employer, "--deadline", deadline, "--salary", salary,
        "--emp-type", emp_type, "--tier", tier, "--verdict", verdict,
        "--batch", batch, "--ref", ref, "--archive", archive, "--note", note,
        "--http", http,
    ]
    if score is not None:
        argv += ["--score", str(score)]
    return run_ops(argv)


@mcp.tool()
def registry_delete(job_id: str, reason: str = "") -> str:
    """🔴 Delete a JD record (high-risk write operation: deletes the whole row and logs it).

    Call only after an explicit user instruction. Deletion cannot be undone
    through this tool - rollback requires the user to restore from a backup.
    """
    argv = ["delete", "--id", job_id]
    if reason:
        argv += ["--reason", reason]
    return run_ops(argv)


@mcp.tool()
def registry_log(msg: str) -> str:
    """Append a note to the operation log (write operation, does not touch master-ledger data rows)."""
    return run_ops(["log", "--msg", msg])


@mcp.tool()
def registry_addcol(name: str, default: str = "", note: str = "") -> str:
    """Append a column at the end of the master-ledger header (structural write; skipped if it already exists)."""
    argv = ["addcol", "--name", name, "--default", default]
    if note:
        argv += ["--note", note]
    return run_ops(argv)


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
