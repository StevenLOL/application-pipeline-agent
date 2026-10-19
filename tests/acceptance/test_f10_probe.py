"""Acceptance: F10 three-way pollers end-to-end (the network-independent part).

Discipline:
- CI/offline-runnable: pollers replay via FakeTransport, store lands in a
  temp DB; real-network probing is triggered only by a human `apa probe run`
  (Task Scheduler resident), never in CI.
- The targets file (data/probe_targets.json) is personal application strategy
  (gitignored); skip when absent — a missing targets file must not turn CI red.
- Host-machine probes (schtasks / conda) follow the skipif pattern from
  test_environment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apa import config
from apa.cli import main
from apa.f10 import pollers, scheduler, store
from apa.f10.http import FakeTransport, HttpResponse

CONDA_EXE = os.environ.get("APA_CONDA", r"E:\Users\Administrator\miniconda3\Scripts\conda.exe")
needs_conda = pytest.mark.skipif(
    not Path(CONDA_EXE).is_file(), reason=f"no conda on host ({CONDA_EXE}) — skipped in CI environments"
)


def test_probe_run_end_to_end_with_fake_transport(tmp_path, capsys, monkeypatch):
    """CLI probe run full chain: probe → persist → summary output (FakeTransport
    injected, real network forbidden)."""
    fake = FakeTransport({
        (config.F10_STATUSPAGE_BASE + "/api/v2/summary.json", "GET"):
            HttpResponse(200, json.dumps({"page": {"status_indicator": "none",
                                                  "status_description": "ok"},
                                          "components": [], "incidents": []})),
        (config.F10_MAINTENANCE_URL, "GET"): HttpResponse(200, "x" * 5000),
    })
    # the CLI does `from .f10.http import UrllibTransport` inside the command
    # function → patch the module attribute
    monkeypatch.setattr("apa.f10.http.UrllibTransport", lambda **kw: fake)

    # ① kind=all but the targets file is missing: the jobs class errors out
    #   with exit code 2 (build the targets first)
    rc = main(["probe", "run", "--kind", "all", "--db", str(tmp_path / "a.db"),
               "--targets", str(tmp_path / "none.json")])
    assert rc == 2
    # ② single class (statuspage) should pass the full chain: probe → persist → summary
    rc = main(["probe", "run", "--kind", "statuspage", "--db", str(tmp_path / "b.db")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "probe complete: 1 results" in out and "[blocked]" not in out
    assert store.count_outcomes(tmp_path / "b.db") == {"ok": 1}


def test_probe_status_and_targets_cli(tmp_path, capsys, monkeypatch):
    """The probe status / targets subcommands run; targets errors with code 2
    when the targets file is missing."""
    db = tmp_path / "apa.db"
    store.write_results(db, [pollers.ProbeResult("job_url", "https://x/1", "ok", "HTTP 200", 10)])
    assert main(["probe", "status", "--db", str(db)]) == 0
    assert "ok" in capsys.readouterr().out
    assert main(["probe", "targets", "--targets", str(tmp_path / "nope.json")]) == 2


def test_probe_sync_health_cli(tmp_path, capsys):
    """probe sync-health: the channel health snapshot is written into the
    application queue's channel_health field.

    A real gap found in the review self-audit (2026-10-19): sync-health was
    implemented but never tested — this subcommand is the data source for the
    portal-health-gate rule; a wrong snapshot means a broken gate, so
    acceptance coverage is mandatory.
    """
    db = tmp_path / "apa.db"
    # two channels, three states (including the neutral blocked observation,
    # which does not count toward the streak)
    store.write_results(db, [
        pollers.ProbeResult("statuspage", config.F10_STATUSPAGE_BASE, "ok", "indicator=none", 10),
        pollers.ProbeResult("job_url", "https://jobs.careers.gov.sg/jobs/hrp/1/x", "blocked", "HTTP 403", 10),
        pollers.ProbeResult("job_url", "https://jobs.careers.gov.sg/jobs/hrp/2/x", "ok", "HTTP 200", 10),
    ])
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"targets": [
        {"jd_id": "GOV-1", "url": "https://jobs.careers.gov.sg/jobs/hrp/1/x",
         "title": "t", "employer": "e", "channel": "hrp", "deadline": None, "verdict": "submittable"},
        {"jd_id": "GOV-2", "url": "https://jobs.careers.gov.sg/jobs/hrp/2/x",
         "title": "t", "employer": "e", "channel": "hrp", "deadline": None, "verdict": "submittable"},
    ]}), encoding="utf-8")
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps({"entries": [
        {"channel": "hrp", "jd_id": "GOV-1", "state": "ready"},
    ]}), encoding="utf-8")

    assert main(["probe", "sync-health", "--db", str(db),
                 "--targets", str(targets), "--queue", str(queue)]) == 0
    out = capsys.readouterr().out
    assert "channel health snapshot written" in out

    data = json.loads(queue.read_text(encoding="utf-8"))
    # the snapshot lands on every entry (portal-health-gate reads per entry)
    entry = data["entries"][0]
    # within the hrp channel: one blocked (neutral, not counted in the streak)
    # + one ok → healthy
    assert entry["channel_health"] == "healthy"
    assert entry["channel_health_since"] is not None

    # missing targets file / missing queue → exit code 2 (fail-loud, never
    # write a half-baked snapshot)
    assert main(["probe", "sync-health", "--db", str(db),
                 "--targets", str(tmp_path / "none.json"),
                 "--queue", str(queue)]) == 2
    assert main(["probe", "sync-health", "--db", str(db),
                 "--targets", str(targets),
                 "--queue", str(tmp_path / "noqueue.json")]) == 2


@pytest.mark.skipif(not config.PROBE_TARGETS_PATH.exists(),
                    reason="probe targets not generated (scripts/03_build_probe_targets.py) — skipped")
def test_probe_targets_file_wellformed():
    """Targets file schema is sound: all URLs are http(s) with no whitespace
    (dirty links are stripped at build time)."""
    data = json.loads(config.PROBE_TARGETS_PATH.read_text(encoding="utf-8"))
    assert data["targets"], "targets list is empty"
    for t in data["targets"]:
        assert t["url"].startswith("http")
        assert not any(c.isspace() for c in t["url"]), f"URL contains whitespace: {t['url']!r}"
        assert t["channel"] in {"hrp", "hrp-greenhouse", "hrp-workable", "mcf", "other"}


def test_register_tasks_dry_run_no_apply(capsys):
    """Dry-run by default: prints the 3 schtasks lines but does not execute
    them (no host changes without --apply)."""
    rc = main(["probe", "register-tasks"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("schchtasks") == 0 and out.count("schtasks /Create") == 3
    assert "dry-run" in out


@needs_conda
def test_scheduler_commands_reference_absolute_apa_exe():
    """The dry-run output's /TR takes an absolute path via --apa-exe (the Task
    Scheduler PATH does not include the conda env)."""
    cmds = scheduler.build_commands(apa_exe=r"E:\Users\Administrator\miniconda3\envs\apa\Scripts\apa.exe")
    assert all("Scripts\\apa.exe" in c.command for c in cmds)
