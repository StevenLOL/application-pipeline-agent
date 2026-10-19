"""R0 review routing: claims -> which ones a human should handle (docs/20 §2.2 criteria table).

**This is the core implementation of docs/20**: routing is a pure function and
unit-testable; the UI only renders the queue it produces. This keeps CLI and UI
behavior identical (docs/11 thin-pointer discipline) and lets the routing rules be
verified without ever launching a UI.

Design basis (not my preferences — four conclusions repeatedly validated in the
industry, sources in docs/20 §1):
1. **Review everything = reviewing nothing** (velam: 400 items/day, median 2s ->
   rubber stamps, not judgment)
   -> the default tier is ``auto``, not "hand everything to a human"
2. **Route by criteria, not by feel** (low confidence / high impact / anomaly / sampling)
3. **policy override confidence** (high-impact fields get human eyes regardless of confidence)
4. **Frame the decision** (give a concrete question, not a generic approve)

This module only does **triage and reason-giving**; it never mutates claim state —
mutation happens only in ``apa.profile.merge`` (single write path).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .models import Claim, ClaimStatus


class Route(str, Enum):
    """Triage tiers. **AUTO is the default** — this is the key to cutting 25 items down to 0-3."""

    AUTO = "auto"          # use directly, no interruption
    CONFIRM = "confirm"    # low-cost confirmation (source available for checking, non-blocking)
    DECIDE = "decide"      # needs a human decision (conflict / identity / high impact / anomaly)


#: Identity fields — **high impact**: a wrong identity value = resume-fraud risk (an
#: A4-class incident). Note work_authorization is an enum, not a boolean.
IDENTITY_FIELDS = frozenset({
    "identity.work_authorization", "identity.full_name",
    "identity.email", "identity.phone",
})

#: High-impact experience fields: a wrong organization or title distorts the whole document.
HIGH_STAKE_PREFIXES = ("experiences.0.org", "experiences.0.title")

#: Confidence floor: below this goes to decide (the docs/20 §2.2 "anomaly" criterion).
#: Structured direct extraction (JSON Resume) gets 0.9; LLM extraction starts at 0.7;
#: each multi-source corroboration adds +0.1.
CONFIDENCE_FLOOR = 0.6


@dataclass
class Routed:
    """The routing result for one claim + **a human-readable reason for the verdict**.

    ``reason`` is the user-facing answer to "why does this one need you" — docs/20
    §1 conclusion: do not hand over a wall of output; explain why the system is
    uncertain.
    """

    claim: Claim
    route: Route
    reason: str

    @property
    def field(self) -> str:
        return self.claim.field

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.claim.field,
            "text": self.claim.text,
            "route": self.route.value,
            "reason": self.reason,
            "confidence": round(self.claim.confidence, 2),
            "status": self.claim.status.value,
            "sources": sorted({p.source_file for p in self.claim.provenance}),
            "locator": self.claim.provenance[0].locator if self.claim.provenance else "",
            "snippet": self.claim.provenance[0].snippet if self.claim.provenance else "",
            "options": self.options(),
            "rejected": [p.snippet for p in self.claim.rejected_sources
                         if p.extractor == "rejected-in-reingest"],
        }

    def options(self) -> list[str]:
        """Conflicting items get **two concrete options**, never "skip" (docs/20 §2.3).

        Returns [] when no human choice is needed (single-source confirmation / auto pass).
        """
        if self.route is not Route.DECIDE:
            return []
        incoming = [p for p in self.claim.rejected_sources
                    if p.extractor == "rejected-in-reingest"]
        if incoming:
            return [self.claim.text, incoming[-1].snippet]
        if self.claim.status is ClaimStatus.NEEDS_HUMAN:
            return [self.claim.text, ""]        # second item = "fill in / not applicable"
        return []


def _sources(claim: Claim) -> set[str]:
    return {p.source_file for p in claim.provenance}


def route_claim(claim: Claim) -> Routed:
    """Routing decision for a single claim (criterion order is priority order)."""
    # ── 1. Human judgment first: already confirmed / manually edited -> auto ──
    if claim.status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
        return Routed(claim, Route.AUTO, "you already settled this")

    # ── 2. Conflict -> decide (the reason must name the two concrete values) ──
    if claim.status is ClaimStatus.CONFLICT:
        incoming = [p for p in claim.rejected_sources
                    if p.extractor == "rejected-in-reingest"]
        if incoming:
            return Routed(claim, Route.DECIDE,
                          f"your documents disagree: \"{claim.text[:40]}\" vs "
                          f"\"{incoming[-1].snippet[:40]}\"")
        return Routed(claim, Route.DECIDE, "values conflict but the second side is gone")

    # ── 3. Human flagged as needing supplementation -> decide ──
    if claim.status is ClaimStatus.NEEDS_HUMAN:
        return Routed(claim, Route.DECIDE, "you flagged this as incomplete")

    # ── 4. No provenance = hallucination -> decide (regardless of confidence) ──
    if not claim.provenance:
        return Routed(claim, Route.DECIDE,
                      "no source in your documents — I cannot show you where this came from")

    # ── 5. Identity field -> decide (policy override confidence, docs/20 §1 conclusion 2) ──
    if claim.field in IDENTITY_FIELDS:
        return Routed(claim, Route.DECIDE,
                      "identity field — a wrong value here is a false claim")

    # ── 6. Abnormally low confidence -> decide ──
    if claim.confidence < CONFIDENCE_FLOOR:
        return Routed(claim, Route.DECIDE,
                      f"low confidence ({claim.confidence:.2f})")

    # ── 7. Multi-source corroboration -> auto ──
    srcs = _sources(claim)
    if len(srcs) > 1:
        return Routed(claim, Route.AUTO, f"{len(srcs)} documents agree")

    # ── 8. High-impact experience field + single source only -> decide (high-impact expected cost) ──
    if claim.field.startswith(HIGH_STAKE_PREFIXES):
        return Routed(claim, Route.DECIDE,
                      "organisation and title carry the most weight in a CV — "
                      "worth your eyes")

    # ── 9. Remaining single source -> confirm (low cost, non-blocking) ──
    return Routed(claim, Route.CONFIRM,
                  f"single source: {next(iter(srcs))} — skim if curious")


def route_all(claims: Iterable[Claim]) -> list[Routed]:
    """All claims -> routing results, sorted decide -> confirm -> auto."""
    order = {Route.DECIDE: 0, Route.CONFIRM: 1, Route.AUTO: 2}
    return sorted((route_claim(c) for c in claims),
                  key=lambda r: (order[r.route], r.field))


@dataclass
class ReviewQueue:
    """Review-queue view (UI and CLI share the same structure)."""

    decide: list[Routed] = field(default_factory=list)
    confirm: list[Routed] = field(default_factory=list)
    auto: list[Routed] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.decide) + len(self.confirm) + len(self.auto)

    @property
    def needs_human(self) -> int:
        return len(self.decide)

    def escalation_rate(self) -> float:
        """Escalation rate (docs/20 §6 health metric).

        >50%  = routing is failing, the bottleneck was rebuilt (velam's
                "you've rebuilt the bottleneck")
        <1%   = the signal has fallen asleep (not "the task is easy" — that is the
                more common explanation and must be investigated)
        """
        return (len(self.decide) / self.total) if self.total else 0.0


def build_queue(profile: Any) -> ReviewQueue:
    """Profile -> review queue."""
    routed = route_all(profile.claims.values())
    q = ReviewQueue()
    for r in routed:
        if r.route is Route.DECIDE:
            q.decide.append(r)
        elif r.route is Route.CONFIRM:
            q.confirm.append(r)
        else:
            q.auto.append(r)
    return q


def health_metrics(queue: ReviewQueue) -> dict[str, Any]:
    """Routing health (docs/20 §6). **CLI-side only, never shown in the UI** (monitoring belongs to the Rich layer)."""
    rate = queue.escalation_rate()
    if queue.total == 0:
        verdict = "no claims yet"
    elif rate > 0.5:
        verdict = "routing is not doing its job — a bottleneck was rebuilt"
    elif rate < 0.01:
        verdict = "signals are asleep, not that the task is easy — check them"
    else:
        verdict = "healthy"
    return {
        "total_claims": queue.total,
        "decide": len(queue.decide),
        "confirm": len(queue.confirm),
        "auto": len(queue.auto),
        "escalation_rate": round(rate, 3),
        "verdict": verdict,
    }
