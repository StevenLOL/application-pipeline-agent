"""Character-budget drafter tests — package-02 regression loop + no-new-claims
+ negative cases.

Acceptance: discover 256 → candidate ≤256 → (simulated user rewrite) →
recheck loop; negative: an unlimited job is not trimmed and not falsely
flagged.
"""

from __future__ import annotations

import pytest

from apa.adapt import (
    FieldLimit,
    LimitSource,
    fit_fields,
    fit_to_budget,
    review_final,
)
from apa.compliance.rules import get_rules

# package-02 scenario reconstruction (INC-2026-1015: each work-experience
# segment ≤256 characters)
INCIDENT_TEXT = (
    "At Zephyr's Central Research Institute I directed AI and digital-transformation "
    "initiatives for a multimodal in-cabin and vehicle product line across "
    "mass-production models, models in development and research simultaneously, "
    "coordinating concurrent projects against delivery and budget commitments with "
    "progress and risks reported to executive stakeholders weekly. When the AI "
    "assistant under-performed on mass-production vehicles, I analysed the audio "
    "data streams, found the cause at source and issued the fix as a product "
    "requirement: speech-recognition accuracy improved by ten percentage points "
    "across mass-produced vehicles."
)


class TestFitToBudget:
    def test_incident_regression_within_256(self):
        """Package-02 regression: overlong experience → candidate ≤256 and
        no-new-claims."""
        lim = FieldLimit("work_experience_1", 256, LimitSource.MEASURED)
        cand = fit_to_budget(INCIDENT_TEXT, lim)
        assert len(cand.text) <= 256
        assert cand.within_budget
        # no-new-claims: every word of the candidate comes from the source text
        original_words = set(INCIDENT_TEXT.lower().split())
        cand_words = set(cand.text.lower().split())
        assert cand_words <= original_words, "the candidate contains words not in the source text (new content generated)"
        # trim transparency: there is a drop record
        assert cand.dropped_sentences

    def test_within_budget_passthrough(self):
        lim = FieldLimit("f", 256, LimitSource.DOM)
        cand = fit_to_budget("Already short.", lim)
        assert cand.text == "Already short." and cand.within_budget

    def test_unverified_no_limit_no_trim(self):
        """Negative case: no limit (unverified) → no trim, no false flag."""
        lim = FieldLimit("f", None, LimitSource.UNVERIFIED)
        cand = fit_to_budget(INCIDENT_TEXT, lim)
        assert cand.text == INCIDENT_TEXT and cand.used == len(INCIDENT_TEXT)

    def test_known_limit_requires_value(self):
        with pytest.raises(ValueError, match="requires source=unverified"):
            FieldLimit("f", None, LimitSource.DOM)

    def test_first_sentence_over_limit_fallback(self):
        """A single sentence over budget → truncation fallback (keeping
        within_budget=False semantically correct)."""
        lim = FieldLimit("f", 50, LimitSource.MEASURED)
        cand = fit_to_budget("x" * 80, lim)
        assert len(cand.text) <= 50


class TestReviewFinal:
    def test_closed_loop_after_simulated_rewrite(self):
        """Closed loop: candidate → simulated user equal-length tweak →
        recheck passes."""
        lim = FieldLimit("work_experience_1", 256, LimitSource.MEASURED)
        cand = fit_to_budget(INCIDENT_TEXT, lim)
        rewritten = cand.text.replace("weekly", "weekly, audited").replace(
            "audited, audited", "audited")  # equal-length tweak (simulated user)
        assert len(rewritten) <= 256, len(rewritten)
        result = review_final(rewritten, lim, rules=get_rules("all"))
        assert result.passed, result.findings

    def test_over_budget_flagged(self):
        lim = FieldLimit("f", 50, LimitSource.MEASURED)
        result = review_final("x" * 80, lim)
        assert not result.passed and result.over_budget_fields == ["f"]

    def test_rules_fire_on_final_text(self):
        """The recheck runs the full rule set: inherited red-line words
        introduced in the entered text → caught."""
        lim = FieldLimit("f", 500, LimitSource.DOM)
        result = review_final("I worked full-time on the deployment, it was cutting-edge.",
                              lim, rules=get_rules("all"))
        codes = {s.split("]")[0].lstrip("[") for _, s in result.findings
                 if s.startswith("[")}
        assert any(c.startswith(("R2", "R6")) for c in codes), codes


class TestFitFields:
    def test_multi_field_batch(self):
        cands = fit_fields(
            {"summary": "x" * 300, "role": "y" * 100},
            [FieldLimit("summary", 256, LimitSource.DOM),
             FieldLimit("role", None, LimitSource.UNVERIFIED)],
        )
        assert len(cands["summary"].text) <= 256
        assert cands["role"].text == "y" * 100  # no limit → no trim
