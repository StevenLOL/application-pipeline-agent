"""probe_log persistence and querying + state-machine evaluation and incident registration
(planning doc section 5, probe_log; incidents rows are produced by the stage-5 state machine —
register on entering outage, fill resolved_at on recovery, and affected_json is computed from the target set).

The write path only does INSERT (append-only, the same discipline as audit_log); queries provide
"the most recent result per target" for CLI status and the state machine to consume.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .. import models
from .pollers import ProbeResult
from . import statemachine as sm


def write_results(db_path: str | Path, results: Iterable[ProbeResult]) -> int:
    """Append probe results; returns the number of rows written."""
    rows = [r.to_row() for r in results]
    if not rows:
        return 0
    conn = models.connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO probe_log (ts, kind, target, outcome, detail, latency_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)", rows)
    finally:
        conn.close()
    return len(rows)


def latest_by_target(db_path: str | Path) -> list[dict]:
    """The most recent probe for each (kind, target) (used by CLI probe status)."""
    conn = models.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT p.kind, p.target, p.ts, p.outcome, p.detail, p.latency_ms"
            " FROM probe_log p JOIN (SELECT kind, target, MAX(id) AS mid"
            "                       FROM probe_log GROUP BY kind, target) m"
            " ON p.kind = m.kind AND p.target = m.target AND p.id = m.mid"
            " ORDER BY p.kind, p.target")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def count_outcomes(db_path: str | Path) -> dict[str, int]:
    conn = models.connect(db_path)
    try:
        cur = conn.execute("SELECT outcome, COUNT(*) FROM probe_log GROUP BY outcome")
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def load_observations(db_path: str | Path) -> list[sm.Observation]:
    """The full probe_log → observation sequence (ascending by ts)."""
    conn = models.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT ts, kind, target, outcome FROM probe_log ORDER BY ts, id")
        out = []
        for r in cur.fetchall():
            try:
                ts = datetime.fromisoformat(r["ts"])
            except (ValueError, TypeError):
                continue
            out.append(sm.Observation(ts=ts, outcome=r["outcome"],
                                      target=f"{r['kind']}|{r['target']}"))
        return out
    finally:
        conn.close()


def channel_of(target_key: str, target_channel: dict[str, str]) -> str:
    """Series key (kind|target) → channel. job_url uses the target mapping; the rest are merged by kind:host."""
    kind, _, target = target_key.partition("|")
    if kind == "job_url":
        return target_channel.get(target, f"unmapped:{target[:40]}")
    host = target.split("/")[2] if "://" in target else target
    return f"{kind}:{host}"


def evaluate(db_path: str | Path,
             target_channel: dict[str, str]) -> dict[str, sm.ChannelState]:
    """Run the state machine over probe_log → {channel: ChannelState} (series granularity in the statemachine docstring)."""
    obs = load_observations(db_path)
    series: dict[str, list[sm.Observation]] = {}
    for o in obs:
        ch = channel_of(o.target, target_channel)
        series.setdefault(ch, []).append(o)
    return sm.channel_states(series)


def evaluate_and_record(db_path: str | Path,
                        target_channel: dict[str, str],
                        jobs: list[sm.JobRef] | None = None,
                        alt_submitted: dict[str, bool] | None = None,
                        batch_jd_ids: set[str] | None = None,
                        n: int = 2) -> dict:
    """State machine + incident registration (idempotent): returns {states, intervals, incidents}.

    incidents: one AUTO-{channel}-{ts} row per outage interval per channel;
    affected_json = affected work and its three-tier classification (jobs provides deadline/channel,
    alt_submitted provides alternative-submission evidence, batch_jd_ids provides evergreen roles'
    batch membership — all from queue entries).
    """
    jobs = jobs or []
    alt = alt_submitted or {}
    batch = batch_jd_ids or set()
    obs = load_observations(db_path)

    series: dict[str, list[sm.Observation]] = {}
    changes_by_target: dict[str, list[sm.StateChange]] = {}
    for o in obs:
        ch = channel_of(o.target, target_channel)
        series.setdefault(ch, []).append(o)
    for ch_obs in series.values():
        by_target: dict[str, list[sm.Observation]] = {}
        for o in ch_obs:
            by_target.setdefault(o.target, []).append(o)
        for t, t_obs in by_target.items():
            changes_by_target[t] = sm.series_timeline(t_obs, n)
    target_channel_full = {t: channel_of(t, target_channel) for t in changes_by_target}
    intervals = sm.channel_outage_intervals(changes_by_target, target_channel_full)
    states = sm.channel_states(series, n)

    incidents = []
    conn = models.connect(db_path)
    try:
        with conn:
            for ch, wins in intervals.items():
                for w in wins:
                    affected = sm.affected_jobs(w, jobs, batch)
                    tiers = {j.jd_id: sm.classify_tier(j, w, alt.get(j.jd_id, False))
                             for j in affected}
                    affected_json = {
                        "window": {"started_at": w.started_at.isoformat(),
                                   "resolved_at": w.resolved_at.isoformat()
                                   if w.resolved_at else None},
                        "channel": ch,
                        "tiers": tiers,
                        "jobs": [{"jd_id": j.jd_id, "title": j.title,
                                  "deadline": j.deadline.isoformat()
                                  if j.deadline else None,
                                  "tier": tiers[j.jd_id]} for j in affected],
                    }
                    code = f"AUTO-{ch}-{w.started_at:%Y%m%dT%H%M}"
                    existing = conn.execute(
                        "SELECT id FROM incidents WHERE code = ?", (code,)).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE incidents SET resolved_at = ?, affected_json = ?"
                            " WHERE id = ?",
                            (w.resolved_at.isoformat() if w.resolved_at else None,
                             json.dumps(affected_json, ensure_ascii=False),
                             existing["id"]))
                    else:
                        conn.execute(
                            "INSERT INTO incidents"
                            " (code, started_at, resolved_at, affected_json, actions_json,"
                            "  report_ref) VALUES (?, ?, ?, ?, ?, ?)",
                            (code, w.started_at.isoformat(),
                             w.resolved_at.isoformat() if w.resolved_at else None,
                             json.dumps(affected_json, ensure_ascii=False),
                             None, None))
                    incidents.append({"code": code, "channel": ch,
                                      "started_at": w.started_at.isoformat(),
                                      "resolved_at": w.resolved_at.isoformat()
                                      if w.resolved_at else None,
                                      "tiers": tiers})
    finally:
        conn.close()
    return {"states": states, "intervals": intervals, "incidents": incidents}


def sync_health(db_path: str | Path, queue_path: str | Path,
                target_channel: dict[str, str]) -> dict[str, str]:
    """Write the current channel health snapshot into the submission queue entries' channel_health field (portal-health-gate reads it).

    Channels with no probe records → "unknown" (gate fail-closed: invisible = fail).
    Returns {channel: state}.
    """
    states = evaluate(db_path, target_channel)
    qp = Path(queue_path)
    data = json.loads(qp.read_text(encoding="utf-8"))
    written = {}
    for e in data.get("entries", []):
        ch = e.get("channel", "")
        state = states[ch].state if ch in states else "unknown"
        e["channel_health"] = state
        e["channel_health_since"] = (states[ch].since.isoformat()
                                     if ch in states and states[ch].since else None)
        written[ch] = state
    qp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return written
