"""F10 queue rule tests (deadline-buffer / portal-health-gate / channel-divergence).

Queue JSON construction helpers + positive/negative cases; non-JSON text must
be silently inapplicable (when `--rules all` runs on cover letters/resumes,
this group must not false-positive).
"""

from __future__ import annotations

import json

import pytest

from apa.compliance.rules.f10 import (f10_rules, _check_channel_divergence,
                                      _check_deadline_buffer,
                                      _check_portal_health_gate)


def _queue(entries: list[dict]) -> str:
    return json.dumps({"version": "d5-minimal", "entries": entries}, ensure_ascii=False)


# ---------- deadline-buffer ----------

class TestDeadlineBuffer:
    def test_missing_personal_deadline_flagged(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp",
                     "official_deadline": "2026-10-30T23:59:59+08:00"}])
        hits = _check_deadline_buffer(q)
        assert len(hits) == 1 and "personal_deadline" in hits[0][1]

    def test_insufficient_buffer_flagged(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp",
                     "official_deadline": "2026-10-30T23:59:59+08:00",
                     "personal_deadline": "2026-10-29T23:59:59+08:00"}])  # only 24h earlier
        hits = _check_deadline_buffer(q)
        assert len(hits) == 1 and "−48h" in hits[0][1]

    def test_exactly_48h_passes(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp",
                     "official_deadline": "2026-10-30T23:59:59+08:00",
                     "personal_deadline": "2026-10-28T23:59:59+08:00"}])
        assert _check_deadline_buffer(q) == []

    def test_non_gov_channel_not_checked(self):
        q = _queue([{"jd_id": "M1", "channel": "mcf",
                     "official_deadline": "2026-10-30T23:59:59+08:00"}])
        assert _check_deadline_buffer(q) == []

    def test_missing_official_deadline_flagged(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp-workable",
                     "personal_deadline": "2026-10-28T23:59:59+08:00"}])
        hits = _check_deadline_buffer(q)
        assert len(hits) == 1 and "official_deadline" in hits[0][1]

    def test_non_json_not_applicable(self):
        assert _check_deadline_buffer("# 这是一封求职信\n\nDear Hiring Manager,") == []


# ---------- portal-health-gate ----------

class TestPortalHealthGate:
    def test_ready_unhealthy_flagged(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp", "status": "ready",
                     "channel_health": "outage"}])
        hits = _check_portal_health_gate(q)
        assert len(hits) == 1 and "portal-health-gate" in hits[0][1]

    def test_ready_healthy_passes(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp", "status": "ready",
                     "channel_health": "healthy"}])
        assert _check_portal_health_gate(q) == []

    def test_ready_missing_snapshot_fails_closed(self):
        """Missing snapshot = unknown = fail (out of sight ≠ healthy)."""
        q = _queue([{"jd_id": "G1", "channel": "hrp", "status": "ready"}])
        hits = _check_portal_health_gate(q)
        assert len(hits) == 1 and "unknown" in hits[0][1]

    def test_not_ready_not_gated(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp", "status": "drafted",
                     "channel_health": "outage"}])
        assert _check_portal_health_gate(q) == []


# ---------- channel-divergence ----------

class TestChannelDivergence:
    def test_gov_via_aggregator_direct_violation(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp", "source": "aggregator"}])
        hits = _check_channel_divergence(q)
        assert len(hits) == 1 and "direct violation" in hits[0][1]

    def test_deadline_mismatch_flagged(self):
        q = _queue([{"jd_id": "M1", "channel": "mcf", "source": "aggregator",
                     "official_deadline": "2026-10-30T23:59:59+08:00",
                     "aggregator_deadline": "2026-10-29T23:59:59+08:00"}])
        hits = _check_channel_divergence(q)
        assert len(hits) == 1 and "≠" in hits[0][1]

    def test_aggregator_matching_deadline_passes(self):
        q = _queue([{"jd_id": "M1", "channel": "mcf", "source": "aggregator",
                     "official_deadline": "2026-10-30T23:59:59+08:00",
                     "aggregator_deadline": "2026-10-30T23:59:59+08:00"}])
        assert _check_channel_divergence(q) == []

    def test_direct_gov_passes(self):
        q = _queue([{"jd_id": "G1", "channel": "hrp", "source": "direct"}])
        assert _check_channel_divergence(q) == []

    def test_non_json_not_applicable(self):
        assert _check_channel_divergence("not json at all") == []


# ---------- rule registration ----------

def test_f10_rules_registered():
    rules = f10_rules()
    ids = {r.id for r in rules}
    assert ids == {"deadline-buffer", "portal-health-gate", "channel-divergence"}
    assert all(r.hard for r in rules)          # gate rules: letting one through replays the accident
    assert all(r.kind == "lambda" for r in rules)
