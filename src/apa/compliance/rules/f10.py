"""F10 queue rules (staged development plan, stage 5: deadline-buffer / portal-health-gate / channel-divergence).

Applies to the **submission queue** (`data/submission_queue.json`, the stage-5 minimal contract, extended in F12):
  {"version": "d5-minimal",
   "entries": [{"jd_id", "channel", "status", "official_deadline",
                "personal_deadline", "source", "aggregator_deadline",
                "channel_health"}, ...]}

The rules are implemented as lambda-kind (no engine changes needed): fn receives the full file text and
returns [] for non-JSON text (the rules do not apply to non-queue files — when `--rules all` runs over
CLs/resumes, this group silently skips).

Severity: all three are hard — they all occur at the threshold of "about to submit"; letting one through
re-enacts INC-2026-1016 (no buffer period colliding with a maintenance window) / INC-2026-1015
(aggregator-site caliber drift).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from ..engine import Rule

# Government portal channels (those going through the official portal in the design doc's seven-way channel
# classification): the buffer-period rule only constrains these. Values = the three ATS backends of
# jobs.careers.gov.sg (hrp/greenhouse/workable) — the status page meters them as independent components,
# and the channel classification is likewise split by backend (scripts/03).
# MCF is an employment job bank, not a submission portal, so it is not in this group (its deadline-drift
# risk belongs to the aggregator rules).
GOV_CHANNELS = {"hrp", "hrp-greenhouse", "hrp-workable"}
# Personal-deadline buffer for gov-portal roles (post-INC-2026-1016 caliber: official deadline −48h)
DEADLINE_BUFFER = timedelta(hours=48)

RULE_DEADLINE_BUFFER = "deadline-buffer"
RULE_PORTAL_HEALTH_GATE = "portal-health-gate"
RULE_CHANNEL_DIVERGENCE = "channel-divergence"


def _parse_queue(text: str) -> list[dict]:
    """Parse the queue text; non-JSON / non-conforming structure → returns [] (rule not applicable, not an error)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def _dt(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _check_deadline_buffer(text: str) -> list[tuple[int, str]]:
    """gov-portal roles must have a personal_deadline no later than official_deadline − 48h."""
    out = []
    for i, e in enumerate(_parse_queue(text)):
        if not isinstance(e, dict) or e.get("channel") not in GOV_CHANNELS:
            continue
        jd = e.get("jd_id", f"#{i}")
        official = _dt(e.get("official_deadline"))
        personal = _dt(e.get("personal_deadline"))
        if official is None:
            out.append((0, f"[{jd}] gov-portal role is missing official_deadline; the buffer period cannot be computed"))
            continue
        if personal is None:
            out.append((0, f"[{jd}] gov-portal role is missing personal_deadline"
                           f" (should be official deadline −48h; INC-2026-1016 lesson)"))
        elif personal > official - DEADLINE_BUFFER:
            out.append((0, f"[{jd}] personal_deadline {personal.isoformat()} is later than "
                           f"official deadline −48h ({official - DEADLINE_BUFFER:%Y-%m-%d %H:%M})"))
    return out


def _check_portal_health_gate(text: str) -> list[tuple[int, str]]:
    """For status=ready work, the channel health snapshot must be healthy (missing=unknown=fail, fail-closed)."""
    out = []
    for i, e in enumerate(_parse_queue(text)):
        if not isinstance(e, dict):
            continue
        if e.get("status") != "ready":
            continue
        health = str(e.get("channel_health") or "unknown")
        if health != "healthy":
            out.append((0, f"[{e.get('jd_id', f'#{i}')}] ready-to-submit but channel health "
                           f"={health} (portal-health-gate: fix the channel first or reschedule; "
                           f"the snapshot is refreshed by apa probe sync-health)"))
    return out


def _check_channel_divergence(text: str) -> list[tuple[int, str]]:
    """A government role going through an aggregator site = a direct violation; aggregator deadline ≠ original deadline = violation (must be traced)."""
    out = []
    for i, e in enumerate(_parse_queue(text)):
        if not isinstance(e, dict):
            continue
        jd = e.get("jd_id", f"#{i}")
        ch = e.get("channel")
        if e.get("source") == "aggregator":
            if ch in GOV_CHANNELS:
                out.append((0, f"[{jd}] government role ({ch}) found via an aggregator site — direct violation "
                               "(F10 channel taxonomy: government roles must go through the official portal)"))
            agg = _dt(e.get("aggregator_deadline"))
            official = _dt(e.get("official_deadline"))
            if agg is not None and official is not None and agg != official:
                out.append((0, f"[{jd}] aggregator deadline {agg.isoformat()} ≠ original "
                               f"{official.isoformat()} (the original takes precedence; update after tracing)"))
    return out


def f10_rules() -> list[Rule]:
    """The three queue rules (hard; provenance notes the source accident / design section)."""
    return [
        Rule(id=RULE_DEADLINE_BUFFER, code="F10-1",
             desc="gov-portal roles must have a personal_deadline (official deadline −48h)",
             kind="lambda", fn=_check_deadline_buffer, hard=True,
             provenance="INC-2026-1016 (the maintenance window swallowed 16 same-day-deadline roles; staged development plan, stage 5)"),
        Rule(id=RULE_PORTAL_HEALTH_GATE, code="F10-2",
             desc="channel health must pass before ready-to-submit (a non-healthy snapshot blocks)",
             kind="lambda", fn=_check_portal_health_gate, hard=True,
             provenance="INC-2026-1016 (portal-health-gate, planning doc section 5 rule catalog)"),
        Rule(id=RULE_CHANNEL_DIVERGENCE, code="F10-3",
             desc="aggregator deadline ≠ original; a government role going through an aggregator is a direct violation",
             kind="lambda", fn=_check_channel_divergence, hard=True,
             provenance="F10/channel taxonomy (planning doc section 5 rule catalog)"),
    ]
