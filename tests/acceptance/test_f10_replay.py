"""Acceptance: INC-SYNTH-1016 timeline replay (evals/f10/replay_inc_synth_1016.json).

Golden replay: fixture (real postings + reconstructed probe timeline) →
probe_log (temp DB) → state machine → incidents registration → affected list
and three-tier classification, asserting fixture.expected item by item.

Discipline: fully offline (temp SQLite); the fixture is a versioned golden
artifact — change the expectation by changing the fixture first, with an
explanation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from apa.f10 import statemachine as sm, store
from apa.f10.pollers import ProbeResult

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "f10" / "replay_inc_synth_1016.json"
pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="replay fixture holds real incident data (public repo: synthetic rebuild pending)",
)


def _expand_probes(fx: dict) -> list[ProbeResult]:
    """Expand the probe timeline into per-scope segments → a ProbeResult list
    (for writing into probe_log).

    Cursor continuation between segments: a segment with "start" resets the
    cursor, otherwise it continues from the last entry of the previous
    segment +step (guaranteeing an observation at the 21:00 window boundary
    and no duplicates or gaps between segments).
    """
    targets = fx["targets"]
    out: list[ProbeResult] = []
    cursors: dict[str, datetime | None] = {}
    for seg in fx["probe_segments"]:
        scope = seg["scope"]
        if scope == "careersgov":
            urls = [t["url"] for t in targets if "jobs.careers.gov.sg" in t["url"]]
        elif scope == "mcf":
            urls = [t["url"] for t in targets if "mycareersfuture.gov.sg" in t["url"]]
        else:
            raise ValueError(f"unknown scope: {scope}")
        step = timedelta(minutes=seg["every_min"])
        if "start" in seg:
            ts = datetime.fromisoformat(seg["start"])
        elif cursors.get(scope) is not None:
            ts = cursors[scope] + step
        else:
            raise ValueError(f"the first segment of scope {scope} must give an explicit start")
        until = datetime.fromisoformat(seg["until"])
        while ts <= until:
            for u in urls:
                out.append(ProbeResult(kind="job_url", target=u, outcome=seg["outcome"],
                                       detail="replay", latency_ms=100,
                                       ts=ts.isoformat()))
            ts += step
        cursors[scope] = ts - step
    return out


@pytest.fixture(scope="module")
def replay_report(tmp_path_factory):
    fx = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db = tmp_path_factory.mktemp("replay") / "apa.db"
    probes = _expand_probes(fx)
    assert store.write_results(db, probes) == len(probes)

    tmap = {t["url"]: t["channel"] for t in fx["targets"]}
    jobs = [sm.JobRef(jd_id=t["jd_id"], channel=t["channel"],
                      deadline=datetime.fromisoformat(t["deadline"]) if t["deadline"] else None,
                      title=t.get("title", ""), employer=t.get("employer", ""))
            for t in fx["targets"]]
    alt = {t["jd_id"]: bool(t.get("alternative_submitted"))
           for t in fx["targets"] if t.get("alternative_submitted")}
    batch = {t["jd_id"] for t in fx["targets"] if t.get("in_batch")}
    report = store.evaluate_and_record(db, tmap, jobs, alt, batch, n=fx["n"])
    return report, db, fx


class TestInc20261016Replay:
    def test_channel_states(self, replay_report):
        """At the timeline end (after recovery) the channel's final state
        should be healthy; the outage facts are carried by the interval
        assertions."""
        report, _, fx = replay_report
        for ch, want in fx["expected"]["channels"].items():
            assert report["states"][ch].state == want["final_state"], ch

    def test_outage_intervals(self, replay_report):
        report, _, fx = replay_report
        for ch, want in fx["expected"]["channels"].items():
            if "interval" not in want:
                continue
            wins = report["intervals"].get(ch, [])
            assert len(wins) == 1, ch
            assert wins[0].started_at.isoformat() == want["interval"][0]
            assert wins[0].resolved_at.isoformat() == want["interval"][1]

    def test_affected_and_tiers(self, replay_report):
        """Core acceptance: the affected list reproduces the three-day posting
        set + Helios judged RECOVERABLE + control postings unaffected."""
        report, _, fx = replay_report
        exp = fx["expected"]
        incidents = {i["channel"]: i for i in report["incidents"]}
        assert set(incidents) == {"hrp", "hrp-workable"}

        hrp_tiers = incidents["hrp"]["tiers"]
        assert sorted(hrp_tiers) == sorted(exp["affected_hrp"])
        wv_tiers = incidents["hrp-workable"]["tiers"]
        assert sorted(wv_tiers) == sorted(exp["affected_hrp_workable"])

        for jd, tier in exp["tiers"].items():
            got = hrp_tiers.get(jd) or wv_tiers.get(jd)
            assert got == tier, f"{jd}: expected {tier}, got {got}"

        all_affected = set(hrp_tiers) | set(wv_tiers)
        for jd in exp["not_affected"]:
            assert jd not in all_affected, f"{jd} should not be affected"

    def test_incident_codes_and_idempotent(self, replay_report):
        """Incident codes match expectations; repeated evaluate does not
        double them (idempotent upsert)."""
        report, db, fx = replay_report
        codes = sorted(i["code"] for i in report["incidents"])
        assert codes == sorted(fx["expected"]["incident_codes"])
        again = store.evaluate_and_record(
            db, {t["url"]: t["channel"] for t in fx["targets"]},
            [sm.JobRef(jd_id=t["jd_id"], channel=t["channel"],
                       deadline=datetime.fromisoformat(t["deadline"]) if t["deadline"] else None)
             for t in fx["targets"]], n=fx["n"])
        assert len(again["incidents"]) == len(report["incidents"])
