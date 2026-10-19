"""F10 state-machine pure-function tests (series_timeline / channel_states /
channel_outage_intervals / affected_jobs / classify_tier).

Coverage: debounce semantics (entering a bad state needs 2 consecutive bad,
recovery needs 2 consecutive good, 1 bad during recovering means relapse),
neutral observations do not count toward streaks, evergreen batch membership,
all branches of the three-tier classification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from apa.f10 import statemachine as sm

TZ = timezone(timedelta(hours=8))


def dt(h: int, m: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 10, day, h, m, tzinfo=TZ)


def obs(ts: datetime, outcome: str, target: str = "t1") -> sm.Observation:
    return sm.Observation(ts=ts, outcome=outcome, target=target)


# ---------- series_timeline: debounce and transitions ----------

class TestSeriesTimeline:
    def test_all_ok_no_changes(self):
        assert sm.series_timeline([obs(dt(1), "ok"), obs(dt(2), "ok")]) == []

    def test_single_bad_no_transition(self):
        """A single failure does not transition (unit version of the
        10-17 double transient-failure scenario)."""
        changes = sm.series_timeline([obs(dt(1), "ok"), obs(dt(2), "unreachable")])
        assert changes == []

    def test_two_consecutive_bad_enters_outage_at_first_bad(self):
        """The since of entering outage = the first observation of the bad
        streak (when the failure actually began, not the confirmation
        moment)."""
        changes = sm.series_timeline([
            obs(dt(20), "ok"), obs(dt(21), "unreachable"), obs(dt(21, 15), "unreachable")])
        assert len(changes) == 1
        assert changes[0].ts == dt(21)
        assert changes[0].to_state == sm.OUTAGE

    def test_two_mild_bad_enters_degraded(self):
        changes = sm.series_timeline([
            obs(dt(1), "ok"), obs(dt(2), "degraded"), obs(dt(3), "degraded")])
        assert changes[-1].to_state == sm.DEGRADED

    def test_degraded_to_outage_needs_severe_evidence(self):
        """degraded accumulating to outage-level evidence → outage (since is
        backdated to the streak start)."""
        changes = sm.series_timeline([
            obs(dt(1), "ok"), obs(dt(2), "degraded"), obs(dt(3), "degraded"),
            obs(dt(4), "outage"), obs(dt(5), "outage")])
        assert changes[-1].to_state == sm.OUTAGE
        assert changes[-1].ts == dt(2)      # backdated: the failure actually began at 02:00

    def test_recovery_two_ok_then_two_more(self):
        """outage →(2 ok) recovering →(2 more ok) healthy."""
        seq = [obs(dt(1), "ok"), obs(dt(2), "unreachable"), obs(dt(3), "unreachable")]
        seq += [obs(dt(4), "ok"), obs(dt(5), "ok"), obs(dt(6), "ok"), obs(dt(7), "ok")]
        changes = sm.series_timeline(seq)
        states = [c.to_state for c in changes]
        assert states == [sm.OUTAGE, sm.RECOVERING, sm.HEALTHY]
        assert changes[1].ts == dt(5) and changes[2].ts == dt(7)

    def test_relapse_single_bad_during_recovering(self):
        """1 bad during recovering means relapse (a half-healed channel is
        not trustworthy)."""
        seq = [obs(dt(1), "unreachable"), obs(dt(2), "unreachable"),
               obs(dt(3), "ok"), obs(dt(4), "ok"), obs(dt(5), "unreachable")]
        changes = sm.series_timeline(seq)
        assert changes[-1].to_state == sm.OUTAGE

    def test_degraded_heals_with_two_ok(self):
        seq = [obs(dt(1), "degraded"), obs(dt(2), "degraded"),
               obs(dt(3), "ok"), obs(dt(4), "ok")]
        changes = sm.series_timeline(seq)
        assert [c.to_state for c in changes] == [sm.DEGRADED, sm.HEALTHY]

    def test_neutral_does_not_break_streak(self):
        """blocked/gone/parse_error carry no evidence: they neither count nor
        break the streak (a long blocked period freezes the state)."""
        seq = [obs(dt(1), "unreachable"), obs(dt(2), "blocked"),
               obs(dt(3), "unreachable"), obs(dt(4), "gone"), obs(dt(5), "parse_error")]
        changes = sm.series_timeline(seq)
        assert changes[-1].to_state == sm.OUTAGE      # 2 consecutive bad still confirm (neutrals are skipped)

    def test_neutral_between_oks_still_heals(self):
        seq = [obs(dt(1), "unreachable"), obs(dt(2), "unreachable"),
               obs(dt(3), "ok"), obs(dt(4), "blocked"), obs(dt(5), "ok"),
               obs(dt(6), "ok"), obs(dt(7), "ok")]
        changes = sm.series_timeline(seq)
        assert [c.to_state for c in changes] == [sm.OUTAGE, sm.RECOVERING, sm.HEALTHY]

    def test_empty_series_healthy(self):
        assert sm.series_final_state([]) == (sm.HEALTHY, None)


# ---------- channel_states / intervals ----------

class TestChannelAggregation:
    def test_channel_states_worst_of_targets(self):
        """The channel takes the worst: one target outage + one target
        healthy → channel outage."""
        ch_obs = {
            "hrp": [obs(dt(1), "unreachable", "a"), obs(dt(2), "unreachable", "a"),
                    obs(dt(1), "ok", "b"), obs(dt(2), "ok", "b")],
        }
        states = sm.channel_states(ch_obs)
        assert states["hrp"].state == sm.OUTAGE

    def test_channel_states_single_transient_stays_healthy(self):
        """Each of the two targets fails once → neither series has 2
        consecutive bad → channel healthy
        (the 2026-10-17 real-data scenario: per-channel aggregation would
        false-positive here)."""
        ch_obs = {
            "hrp-greenhouse": [obs(dt(1), "unreachable", "a"),
                               obs(dt(1), "ok", "b"),
                               obs(dt(2), "ok", "b"),
                               obs(dt(3), "ok", "a")],
        }
        assert sm.channel_states(ch_obs)["hrp-greenhouse"].state == sm.HEALTHY

    def test_outage_interval_spans_first_bad_to_full_heal(self):
        changes = {
            "a": [sm.StateChange(dt(21), sm.HEALTHY, sm.OUTAGE),
                  sm.StateChange(dt(8, 30, 19), sm.OUTAGE, sm.RECOVERING),
                  sm.StateChange(dt(8, 45, 19), sm.RECOVERING, sm.HEALTHY)],
            "b": [sm.StateChange(dt(21, 30), sm.HEALTHY, sm.OUTAGE),
                  sm.StateChange(dt(8, 30, 19), sm.OUTAGE, sm.RECOVERING),
                  sm.StateChange(dt(8, 45, 19), sm.RECOVERING, sm.HEALTHY)],
        }
        intervals = sm.channel_outage_intervals(changes, {"a": "hrp", "b": "hrp"})
        assert len(intervals["hrp"]) == 1
        w = intervals["hrp"][0]
        assert w.started_at == dt(21)                 # earliest entry
        assert w.resolved_at == dt(8, 45, 19)         # all recovered

    def test_unhealed_outage_stays_open(self):
        changes = {"a": [sm.StateChange(dt(21), sm.HEALTHY, sm.OUTAGE)]}
        intervals = sm.channel_outage_intervals(changes, {"a": "hrp"})
        assert intervals["hrp"][0].resolved_at is None


# ---------- affected_jobs ----------

def _job(jd_id: str, channel: str = "hrp", deadline: datetime | None = None) -> sm.JobRef:
    return sm.JobRef(jd_id=jd_id, channel=channel, deadline=deadline)


WINDOW = sm.IncidentWindow(started_at=dt(21), resolved_at=dt(8, 0, 19), channel="hrp")


class TestAffectedJobs:
    def test_deadline_in_window(self):
        jobs = [_job("in", deadline=dt(23, 59)), _job("in2", deadline=dt(23, 59, 17))]
        got = sm.affected_jobs(WINDOW, jobs)
        assert [j.jd_id for j in got] == ["in", "in2"]

    def test_boundaries_excluded(self):
        jobs = [_job("before", deadline=dt(20, 0)),      # before the window start
                _job("after", deadline=dt(23, 59, 20))]  # after the window end
        assert sm.affected_jobs(WINDOW, jobs) == []

    def test_channel_mismatch_excluded(self):
        jobs = [_job("mcf", channel="mcf", deadline=dt(23, 59))]
        assert sm.affected_jobs(WINDOW, jobs) == []

    def test_evergreen_in_batch_included(self):
        jobs = [_job("eg"), _job("eg-nobatch")]
        got = sm.affected_jobs(WINDOW, jobs, batch_jd_ids={"eg"})
        assert [j.jd_id for j in got] == ["eg"]

    def test_evergreen_without_batch_excluded(self):
        assert sm.affected_jobs(WINDOW, [_job("eg")]) == []

    def test_ongoing_window_takes_future_deadlines(self):
        open_w = sm.IncidentWindow(started_at=dt(21), resolved_at=None, channel="hrp")
        jobs = [_job("future", deadline=dt(23, 59, 25))]
        assert [j.jd_id for j in sm.affected_jobs(open_w, jobs)] == ["future"]


# ---------- classify_tier ----------

class TestClassifyTier:
    def test_alternative_submitted_recoverable(self):
        assert sm.classify_tier(_job("x", deadline=dt(23, 59)), WINDOW,
                                alternative_submitted=True) == sm.RECOVERABLE

    def test_evergreen_recoverable_or_at_risk(self):
        eg = _job("eg")
        assert sm.classify_tier(eg, WINDOW, True) == sm.RECOVERABLE
        assert sm.classify_tier(eg, WINDOW, False) == sm.AT_RISK   # still open, worst-case delay

    def test_deadline_before_resolution_lost(self):
        assert sm.classify_tier(_job("x", deadline=dt(23, 59)), WINDOW) == sm.LOST

    def test_deadline_after_resolution_recoverable(self):
        assert sm.classify_tier(_job("x", deadline=dt(23, 59, 19)), WINDOW) == sm.RECOVERABLE

    def test_ongoing_at_risk(self):
        open_w = sm.IncidentWindow(started_at=dt(21), resolved_at=None, channel="hrp")
        assert sm.classify_tier(_job("x", deadline=dt(23, 59, 25)), open_w) == sm.AT_RISK
