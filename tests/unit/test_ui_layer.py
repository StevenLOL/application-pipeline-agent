"""UI-layer tests: monitor rendering (pure functions) + webapp data assembly /
HITL record-keeping.

NiceGUI's UI components are not tested (the framework ships its own testing
capabilities; the minimal version does not introduce them); what is under
test is the data-assembly pure functions and the HITL persistence discipline
— the UI is only a presentation layer (architecture iron rule).
"""

from __future__ import annotations

import json

import pytest

from apa import monitor, webapp


# ---- monitor (Rich rendering pure functions: no crash + correct content) ----

def _render_text(frame) -> str:
    """Rich object → plain text (Console capture)."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(file=buf, width=140).print(frame)
    return buf.getvalue()


def test_monitor_render_with_data(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    from apa.f10 import pollers, store

    store.write_results(db, [
        pollers.ProbeResult("statuspage", "https://status/x", "ok", "indicator=none", 5),
        pollers.ProbeResult("job_url", "https://jobs.careers.gov.sg/jobs/hrp/1/x", "blocked", "HTTP 403", 5),
    ])
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps({"entries": [
        {"jd_id": "GOV-1", "channel": "hrp", "state": "ready",
         "deadline": "2026-10-25", "channel_health": "healthy"},
    ]}), encoding="utf-8")
    text = _render_text(monitor.render(db, queue, {"statuspage": "statuspage", "hrp": "hrp"}))
    assert "STATUSPAGE" in text.upper() and "HRP" in text.upper()
    assert "GOV-1" in text


def test_monitor_render_empty(tmp_path):
    text = _render_text(monitor.render(tmp_path / "none.db", tmp_path / "none.json", {}))
    assert "(no probe records)" in text


# ---- webapp data assembly (pure functions) ----

def test_load_queue_sorted_by_deadline(tmp_path):
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"entries": [
        {"jd_id": "B", "deadline": "2026-10-25"},
        {"jd_id": "A", "deadline": "2026-10-20"},
        {"jd_id": "C", "deadline": ""},   # no deadline → sinks to the bottom
    ]}), encoding="utf-8")
    rows = webapp.load_queue(q)
    assert [r["jd_id"] for r in rows] == ["A", "B", "C"]


def test_load_queue_missing_file(tmp_path):
    assert webapp.load_queue(tmp_path / "none.json") == []


def test_load_channel_states_missing_db(tmp_path):
    assert webapp.load_channel_states(tmp_path / "none.db", {"hrp": "hrp"}) == []


# ---- HITL decision persistence (audit discipline) ----

def test_hitl_decision_appends_jsonl(tmp_path):
    log = tmp_path / "decisions.jsonl"
    e1 = webapp.record_hitl_decision("GOV-1", "approve", "", log)
    e2 = webapp.record_hitl_decision("GOV-2", "waive", "薪资 undisclosed", log)
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert e1["action"] == "approve" and e1["decision_by"].startswith("human")
    assert e2["reason"] == "薪资 undisclosed"
    assert webapp.load_recent_decisions(log, 1)[0]["jd_id"] == "GOV-2"


def test_hitl_waive_requires_reason(tmp_path):
    with pytest.raises(ValueError, match="waive requires a reason"):
        webapp.record_hitl_decision("GOV-1", "waive", "  ", tmp_path / "d.jsonl")


def test_hitl_unknown_action_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown HITL action"):
        webapp.record_hitl_decision("GOV-1", "auto-submit", "", tmp_path / "d.jsonl")
