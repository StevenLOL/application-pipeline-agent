"""F10 state machine + affected-jobs triage + three-tier classification
(staged development plan, stage 5; planning doc section 3, F10).

Pure-function layer (no IO): this module consumes observation sequences and
job references and emits states/lists/tiers - unit-testable and replayable.
DB reads/writes live in detect_incidents (a thin wrapper).

## Series-granularity decision (measured 2026-10-18)

The state machine evaluates **per-(kind, target) series**; a channel's state
is the worst of its target series. Not aggregating per channel has evidence:
on the stage-4 first run (10-17), two different greenhouse targets each saw a
single unreachable (transient SSL jitter, re-probe returned 200 twice) - a
channel-level aggregation would have counted "2 consecutive bad" and mis-entered
degraded/outage; with per-target series + 2-consecutive confirmation, each
one-off failure is a single observation, no transition fires, and the channel
stays healthy. During a real portal outage (INC-2026-1016) every target keeps
failing and every series confirms escalation - detection sensitivity is
unaffected; jitter resistance comes from granularity, not thresholds.

## Debounce semantics (consecutive n=2)

- Entering a bad state needs n consecutive bad observations
  (healthy->degraded/outage; degraded->outage additionally requires outage-grade
  evidence among the bad observations); a channel's outage `since` = the first
  observation of that bad streak (when the failure actually started, not when
  it was confirmed)
- Recovery: degraded->healthy needs n consecutive ok; outage->recovering needs
  n; recovering->healthy needs n more; **a single bad observation during
  recovering relapses to outage** (a half-healed channel is not trusted:
  entering a bad state requires confirmation, leaving recovery requires
  stability, relapse requires sensitivity)
- Neutral observations (blocked/parse_error/gone) count toward no streak -
  no evidence is neither good nor bad; the streak is preserved as-is
  (a long blocked period freezes the state = the probe cannot see, we do not
  invent)

## Three-tier classification (affected jobs)

- RECOVERABLE: an alternative channel was already used (email etc., with
  evidence) -> damage mitigated; or the window ended before the deadline
  (submission still possible after recovery)
- LOST: the deadline falls inside the window with no alternative submission
  -> the opportunity is actually lost (**only for postings with a deadline**;
  evergreen postings cannot be LOST - the job is still open)
- AT-RISK: the window is ongoing and the deadline has not passed -> outcome
  unknown, keep watching; evergreen postings without an alternative also land
  here (can be deferred, cannot be lost)

## Affectedness for evergreen postings

Postings without a deadline cannot satisfy "deadline inside the window";
affectedness is decided by batch membership: a submission batch (queue
entries with status in {drafted, ready}) that covers the failed channel
counts as affected (that submission attempt was blocked).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# States (planning doc section 3, F10: healthy->degraded->outage->recovering)
HEALTHY = "healthy"
DEGRADED = "degraded"
OUTAGE = "outage"
RECOVERING = "recovering"

_STATE_RANK = {HEALTHY: 0, RECOVERING: 1, DEGRADED: 2, OUTAGE: 3}

# Three-tier classification
LOST = "LOST"
AT_RISK = "AT-RISK"
RECOVERABLE = "RECOVERABLE"

# Observation -> severity (None = neutral, no evidence)
_GRADE = {"ok": 0, "degraded": 1, "outage": 2, "unreachable": 2}


def grade(outcome: str) -> int | None:
    """Probe outcome -> severity: 0=fine / 1=impaired / 2=down; None=neutral."""
    return _GRADE.get(outcome)


@dataclass(frozen=True)
class Observation:
    ts: datetime          # observation time (timezone-aware)
    outcome: str          # probe_log.outcome enum
    target: str           # URL or endpoint (series key)


@dataclass(frozen=True)
class StateChange:
    ts: datetime
    from_state: str
    to_state: str


@dataclass(frozen=True)
class ChannelState:
    channel: str
    state: str
    since: datetime
    detail: str = ""


@dataclass(frozen=True)
class IncidentWindow:
    started_at: datetime
    resolved_at: datetime | None      # None = ongoing
    channel: str


@dataclass(frozen=True)
class JobRef:
    jd_id: str
    channel: str
    deadline: datetime | None
    title: str = ""
    employer: str = ""


def series_timeline(obs: list[Observation], n: int = 2) -> list[StateChange]:
    """Single-series state machine: returns state-change points (obs ascending
    by ts; no bad observations = empty list = healthy)."""
    if not obs:
        return []
    state = HEALTHY
    good_streak = bad_streak = 0
    bad_severity = 0
    bad_since: datetime | None = None
    changes: list[StateChange] = []

    for o in obs:
        g = grade(o.outcome)
        if g is None:                      # neutral: does not count and does not interrupt
            continue
        if g == 0:
            good_streak += 1
            bad_streak, bad_severity, bad_since = 0, 0, None
        else:
            if bad_streak == 0:
                bad_since = o.ts
            bad_streak += 1
            bad_severity = max(bad_severity, g)
            good_streak = 0

        if state == HEALTHY and bad_streak >= n:
            state = OUTAGE if bad_severity >= 2 else DEGRADED
            changes.append(StateChange(bad_since, HEALTHY, state))
        elif state == DEGRADED:
            if bad_streak >= n and bad_severity >= 2:
                changes.append(StateChange(bad_since, DEGRADED, OUTAGE))
                state = OUTAGE
            elif good_streak >= n:
                changes.append(StateChange(o.ts, DEGRADED, HEALTHY))
                state = HEALTHY
        elif state == OUTAGE:
            if good_streak >= n:
                changes.append(StateChange(o.ts, OUTAGE, RECOVERING))
                state = RECOVERING
                good_streak = 0      # the two recovery segments are confirmed independently: the triggering ok does not count toward the next segment
        elif state == RECOVERING:
            if bad_streak >= 1:            # a single bad during half-recovery relapses
                changes.append(StateChange(o.ts, RECOVERING, OUTAGE))
                state = OUTAGE
            elif good_streak >= n:
                changes.append(StateChange(o.ts, RECOVERING, HEALTHY))
                state = HEALTHY
    return changes


def series_final_state(obs: list[Observation], n: int = 2) -> tuple[str, datetime | None]:
    """Single-series terminal state and entry time (no observations → healthy/None)."""
    changes = series_timeline(obs, n)
    if not changes:
        return HEALTHY, None
    return changes[-1].to_state, changes[-1].ts


def channel_states(series: dict[str, list[Observation]], n: int = 2) -> dict[str, ChannelState]:
    """channel → state: first expand each series by target to get its terminal state; the channel takes the worst
    (outage>degraded>recovering>healthy).

    series: {channel: [Observation, ...]} (the target identifier is in Observation.target,
    split by target inside this function — **must not** mix a channel's observations into a single series, which is
    exactly the per-channel aggregation the 2026-10-18 evidence warns against: one-off failures on two targets would
    be mis-merged into "2 consecutive bad" and falsely report an outage).
    """
    out: dict[str, ChannelState] = {}
    for ch, obs in series.items():
        by_target: dict[str, list[Observation]] = {}
        for o in obs:
            by_target.setdefault(o.target, []).append(o)
        best, best_since = HEALTHY, None
        for t_obs in by_target.values():
            state, since = series_final_state(t_obs, n)
            if _STATE_RANK[state] > _STATE_RANK[best]:
                best, best_since = state, since
        if best == HEALTHY and obs:
            best_since = obs[0].ts
        out[ch] = ChannelState(
            ch, best, best_since or datetime.min.replace(tzinfo=timezone.utc))
    return out


def channel_outage_intervals(
        changes_by_target: dict[str, list[StateChange]],
        target_channel: dict[str, str],
) -> dict[str, list[IncidentWindow]]:
    """Derive each channel's outage intervals from the change points of each target.

    A channel's timeline = the pointwise worst across its targets' timelines; an outage interval = [entering outage,
    the channel recovering to healthy] (unrecovered means resolved_at=None, i.e. an unresolved incident).
    """
    # aggregate each channel's (ts, target, to_state)
    per_channel: dict[str, list[tuple[datetime, str]]] = {}
    for target, changes in changes_by_target.items():
        ch = target_channel.get(target, target)
        for c in changes:
            per_channel.setdefault(ch, []).append((c.ts, c.to_state))
    intervals: dict[str, list[IncidentWindow]] = {}
    for ch, points in per_channel.items():
        points.sort()
        open_start: datetime | None = None
        last_state = HEALTHY
        for ts, to_state in points:
            last_state = to_state
            if to_state == OUTAGE and open_start is None:
                open_start = ts
            elif to_state == HEALTHY and open_start is not None:
                intervals.setdefault(ch, []).append(
                    IncidentWindow(open_start, ts, ch))
                open_start = None
        if open_start is not None:
            intervals.setdefault(ch, []).append(IncidentWindow(open_start, None, ch))
    return intervals


def affected_jobs(window: IncidentWindow, jobs: list[JobRef],
                  batch_jd_ids: set[str] | None = None) -> list[JobRef]:
    """Affected work = work whose deadline falls inside the window ∪ evergreen work in the batch.

    Inside the window = [started_at, resolved_at] (when resolved_at=None, = [started_at, ∞)).
    evergreen roles (deadline=None) are judged by batch membership: jd_id ∈ batch_jd_ids
    (drafted/ready entries in the queue, i.e. the current submission batch) and a matching channel → affected.
    """
    batch = batch_jd_ids or set()
    out = []
    for j in jobs:
        if j.channel != window.channel:
            continue
        if j.deadline is None:
            if j.jd_id in batch:
                out.append(j)
            continue
        if j.deadline < window.started_at:
            continue
        if window.resolved_at is not None and j.deadline > window.resolved_at:
            continue
        out.append(j)
    # entries with an empty deadline sort last (a tz-aware sentinel, avoiding naive/aware comparison TypeError)
    return sorted(out, key=lambda j: (j.deadline or datetime.max.replace(tzinfo=timezone.utc),
                                      j.jd_id))


def classify_tier(job: JobRef, window: IncidentWindow,
                  alternative_submitted: bool = False) -> str:
    """Single-work three-tier classification (decision rules in the module docstring)."""
    if job.deadline is None:
        # evergreen: the job is still open; the worst case is a delay, it cannot be lost
        return RECOVERABLE if alternative_submitted else AT_RISK
    if alternative_submitted:
        return RECOVERABLE                    # mitigated (a portal make-up submission is a completion action, not a new opportunity)
    if window.resolved_at is None:
        return AT_RISK                        # ongoing: outcome unknown
    if job.deadline >= window.resolved_at:
        return RECOVERABLE                    # the portal recovered before the deadline: normal submission is still possible
    return LOST                               # the deadline falls inside the window with no alternative submission
