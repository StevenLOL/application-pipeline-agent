"""R0 review-routing tests (docs/20).

The core discipline pinned down here: **the default tier is auto, not "hand
everything to a human"**. Evidence: a real run over the 25 draft / single-source
claims in `data/profile.json` would present 25 cards under a flat design;
under this module's routing, only a minority belong in the decide queue.

Criteria priority (docs/20 §2.2): human judgement > conflict > manual flag >
no provenance > identity fields > low confidence > multi-source agreement >
high-impact single source > remaining single sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.profile.models import Claim, ClaimStatus, Profile, Provenance
from apa.profile.router import (CONFIDENCE_FLOOR, ReviewQueue, Route, build_queue,
                                health_metrics, route_all, route_claim)

NOW = "2026-10-19T00:00:00+00:00"


def claim(field: str = "skills.0", text: str = "Python", status: ClaimStatus = ClaimStatus.DRAFT,
          confidence: float = 0.7, sources: tuple[str, ...] = ("cv.docx",),
          rejected: list[Provenance] | None = None) -> Claim:
    """Build one claim.

    Note: a **conflict** carrying ``rejected`` must also be in ``CONFLICT``
    status — on the real path both are produced together by
    ``apa.profile.merge.merge_claims`` (this is not a test-only special case);
    requiring it explicitly here avoids writing the illegal combination of
    "rejected provenance recorded but status is draft".
    """
    rejected = rejected or []
    if rejected and status is ClaimStatus.DRAFT:
        status = ClaimStatus.CONFLICT
    return Claim(
        field=field, text=text, status=status, confidence=confidence,
        provenance=[Provenance(src, f"para {i}", text[:80], NOW)
                    for i, src in enumerate(sources)],
        rejected_sources=rejected)


def incoming_snippet(text: str) -> Provenance:
    """Build a "new value brought in by re-ingest" marker (product of merge's conflict path)."""
    return Provenance("cv2.pdf", "page 2", text, NOW,
                      extractor="rejected-in-reingest")


# ================= single-claim routing =================

class TestRouteClaim:
    def test_settled_claims_are_auto(self):
        for status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
            assert route_claim(claim(status=status)).route is Route.AUTO

    def test_conflict_goes_to_decide_with_both_values_named(self):
        """docs/20 §1 conclusion 4: the reason must name both concrete values, not just "there is a conflict"."""
        c = claim(field="identity.location", text="Singapore",
                  rejected=[incoming_snippet("Tianjin")])
        r = route_claim(c)
        assert r.route is Route.DECIDE
        assert "Singapore" in r.reason and "Tianjin" in r.reason
        assert r.options() == ["Singapore", "Tianjin"]

    def test_needs_human_goes_to_decide(self):
        r = route_claim(claim(status=ClaimStatus.NEEDS_HUMAN))
        assert r.route is Route.DECIDE and "flagged" in r.reason

    def test_no_provenance_is_always_decide_even_if_confident(self):
        """No provenance = hallucination suspect. Even high confidence needs a human look."""
        c = claim(confidence=0.99)
        c.provenance = []
        r = route_claim(c)
        assert r.route is Route.DECIDE and "no source" in r.reason

    @pytest.mark.parametrize("field", sorted(
        {"identity.full_name", "identity.email", "identity.phone",
         "identity.work_authorization"}))
    def test_identity_fields_override_confidence(self, field):
        """policy overrides confidence: identity fields need a human decision no matter how confident."""
        r = route_claim(claim(field=field, confidence=0.99))
        assert r.route is Route.DECIDE and "identity" in r.reason

    def test_low_confidence_goes_to_decide(self):
        r = route_claim(claim(confidence=CONFIDENCE_FLOOR - 0.01))
        assert r.route is Route.DECIDE and "low confidence" in r.reason

    def test_confidence_exactly_at_floor_is_not_decide(self):
        r = route_claim(claim(confidence=CONFIDENCE_FLOOR))
        assert r.route is Route.CONFIRM

    def test_multi_source_agreement_is_auto(self):
        r = route_claim(claim(sources=("cv.docx", "resume.pdf")))
        assert r.route is Route.AUTO and "2 documents agree" in r.reason

    def test_high_stake_single_source_goes_to_decide(self):
        r = route_claim(claim(field="experiences.0.org", text="AdventureWorks"))
        assert r.route is Route.DECIDE and "organisation" in r.reason

    def test_ordinary_single_source_is_confirm_not_decide(self):
        """This is the correct answer to "tedious": ordinary fields must not block the user."""
        r = route_claim(claim(field="experiences.1.bullets.0"))
        assert r.route is Route.CONFIRM and "single source" in r.reason

    def test_decide_never_offers_a_skip_option(self):
        """docs/20 §2.3: decide offers exactly two concrete options, never a "skip"."""
        r = route_claim(claim(field="identity.full_name"))
        assert len(r.options()) <= 2
        assert "" not in r.options() or r.claim.status is ClaimStatus.NEEDS_HUMAN

    def test_non_decide_has_no_options(self):
        assert route_claim(claim()).options() == []


# ================= queue =================

class TestBuildQueue:
    def test_draft_soup_routes_mostly_to_confirm_not_decide(self):
        """Real observed shape: 25 drafts from a single source -> decide should be the minority.

        This is a direct rejection of docs/19 §4.3 "flatten every claim".
        """
        p = Profile()
        for i in range(25):
            p.claims[f"experiences.0.bullets.{i}"] = claim(
                field=f"experiences.0.bullets.{i}")
        q = build_queue(p)
        assert q.needs_human == 0                 # no identity fields, no conflict -> nothing blocks
        assert len(q.confirm) == 25
        assert len(q.auto) == 0
        assert q.escalation_rate() == 0.0

    def test_queue_partitions_everything(self):
        p = Profile()
        p.claims["k_a"] = claim(field="skills.0")                       # confirm
        p.claims["k_b"] = claim(field="skills.1", sources=("x", "y"))   # auto
        p.claims["k_c"] = claim(field="identity.email")                  # decide
        p.claims["k_d"] = claim(field="skills.2",
                                status=ClaimStatus.CONFIRMED)           # auto
        q = build_queue(p)
        assert q.total == 4
        assert len(q.decide) + len(q.confirm) + len(q.auto) == 4
        assert {r.field for r in q.decide} == {"identity.email"}
        assert {r.field for r in q.auto} == {"skills.1", "skills.2"}
        assert {r.field for r in q.confirm} == {"skills.0"}

    def test_empty_profile_has_zero_rate_not_divide_by_zero(self):
        q = build_queue(Profile())
        assert q.total == 0 and q.escalation_rate() == 0.0

    def test_route_all_sorts_decide_first(self):
        p = Profile()
        p.claims["z_confirm"] = claim(field="skills.9")
        p.claims["a_decide"] = claim(field="identity.full_name")
        order = [r.route for r in route_all(p.claims.values())]
        assert order[0] is Route.DECIDE


# ================= health metrics =================

class TestHealthMetrics:
    def test_healthy_band(self):
        p = Profile()
        for i in range(10):
            p.claims[f"k{i}"] = claim(field=f"skills.{i}")
        p.claims["k_id"] = claim(field="identity.email")
        p.claims["k_id2"] = claim(field="identity.phone")
        m = health_metrics(build_queue(p))
        assert m["total_claims"] == 12 and m["decide"] == 2
        assert m["escalation_rate"] == round(2 / 12, 3) and m["verdict"] == "healthy"

    def test_bottleneck_rebuilt_is_called_out(self):
        """velam: 60% escalate = 'you have rebuilt the bottleneck'."""
        p = Profile()
        for f in ("identity.email", "identity.phone", "identity.full_name",
                  "identity.work_authorization"):
            p.claims[f] = claim(field=f)
        m = health_metrics(build_queue(p))
        assert m["escalation_rate"] == 1.0
        assert "bottleneck" in m["verdict"]

    def test_signals_asleep_is_called_out(self):
        """<1% escalate does not mean "the tasks are easy" — usually it means the signals are asleep."""
        p = Profile()
        for i in range(300):
            p.claims[f"skills.{i}"] = claim(field=f"skills.{i}",
                                             sources=("a", "b"))
        m = health_metrics(build_queue(p))
        assert m["escalation_rate"] == 0.0 and "asleep" in m["verdict"]

    def test_no_claims_says_so(self):
        assert health_metrics(build_queue(Profile()))["verdict"] == "no claims yet"


# ================= interface consistency with store / UI =================

class TestQueueSerialisation:
    def test_routed_to_dict_carries_reason_and_options(self):
        c = claim(field="identity.location", text="Singapore",
                  rejected=[incoming_snippet("Tianjin")])
        d = route_claim(c).to_dict()
        assert d["route"] == "decide"
        assert d["options"] == ["Singapore", "Tianjin"]
        assert d["rejected"] == ["Tianjin"]
        assert json.loads(json.dumps(d))["reason"]           # serialisable

    def test_reason_always_non_empty(self):
        """docs/20 §1: every entry must state "why this needs you", none may ride the queue for free."""
        variants = [
            claim(),
            claim(field="identity.full_name"),
            claim(sources=("a", "b")),
            claim(confidence=0.1),
            claim(status=ClaimStatus.CONFIRMED),
            claim(field="experiences.0.org"),
        ]
        for c in variants:
            assert route_claim(c).reason.strip()

    def test_real_profile_file_routes_sanely(self):
        """If data/profile.json exists, run a regression against its real shape."""
        from apa.profile.models import Profile as P

        path = Path(__file__).resolve().parents[2] / "data" / "profile.json"
        if not path.exists():
            pytest.skip("no live profile.json")
        p = P.from_dict(json.loads(path.read_text(encoding="utf-8")))
        q = build_queue(p)
        # a real profile must yield a non-trivial queue and must not dump every claim into decide
        assert q.total == len(p.claims)
        assert q.needs_human < q.total or q.needs_human == 0
        assert all(r.reason.strip() for r in q.decide + q.confirm + q.auto)
