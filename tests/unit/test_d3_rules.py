"""D3 rule unit tests: cross-block-dupe (incident 2) + claim-verification
(incident 4) + cl-resume-consistency.

Division of labour: tests/golden/ validates **version pairs** (incident state
must hit / fixed state silent, from real history); this file validates **rule
mechanism boundaries** (structural block exclusion, quote stripping, alias
normalisation, tool-tenure vs total-tenure calibration, etc.) -- synthetic text
is used only for boundary validation, not as incident evidence.
"""

from __future__ import annotations

import pytest

from apa.compliance.rules.accidents import (
    claim_verification_check, cl_resume_consistency_check, cross_block_dupe_check,
    number_provenance_check,
)

# ── Synthetic CL skeleton (letterhead / salutation / sign-off are structural blocks, excluded from cross-block duplication) ────────

_HEADER = """# Cover Letter

**[CANDIDATE]**
Singapore Citizen
+86 138-0000-0000 · candidate@example.com · github.com/example

16 October 2026

Hiring Manager
Some Agency
Singapore

**Re: Some Role**

Dear Hiring Manager,
"""

_FOOTER = """

Yours faithfully,
**[CANDIDATE]**
"""


def _cl(*body_blocks: str) -> str:
    return _HEADER + "\n\n" + "\n\n".join(body_blocks) + _FOOTER


# ── cross-block-dupe (incident 2) ──────────────────────────────────────────

def test_dupe_flags_independent_work_claim_in_two_blocks():
    """C1 shape: an independent work framework restated once in the body block and once in the closing block -> must hit."""
    text = _cl(
        "Your posting names accountability. I stayed with problems until they were measured.",
        "**On leading AI-native transformation.** Since 2023 I have worked independently: "
        "a governed agentic workflow with human-in-the-loop approval.",
        "Between roles I have been building independently — public systems on GitHub — "
        "and I am looking to bring that back inside an organisation.",
    )
    hits = cross_block_dupe_check(text)
    assert any("independent-work" in seg for _, seg in hits), hits


def test_dupe_single_passing_callback_not_flagged():
    """A single passing callback in the closing block is not duplication -- the same claim holds in only one block."""
    text = _cl(
        "Your posting names accountability. I stayed with problems until they were measured.",
        "**On being accountable.** At Zephyr I owned a multimodal product line and traced "
        "a failing assistant to its cause in the audio data.",
        "Between roles I have been building independently, and I am returning to Singapore "
        "with my family by the end of October.",
    )
    assert cross_block_dupe_check(text) == []


def test_dupe_verbatim_shingle_across_blocks_flagged():
    """Unknown-shape verbatim repetition (>=6-word shingle shared across blocks) -> verbatim detector as fallback."""
    shared = ("we set target costs and negotiated reductions into the suppliers "
              "commercial terms before award")
    text = _cl(
        "**On sourcing.** I ran full-cycle tendering for in-cabin modules: " + shared + ".",
        "**On delivery.** At Evergrande I chaired technical evaluations where again " + shared + ".",
    )
    hits = cross_block_dupe_check(text)
    assert any("verbatim" in seg for _, seg in hits), hits


def test_dupe_quote_span_not_counted():
    """Quoted text (『…』) appearing across blocks is an intentional quote (the threshold gates against the JD's original sentence), not duplication."""
    quoted = "『use AI-native tools and methods to achieve impact within 3-6 months』"
    text = _cl(
        "**On the gate.** The requirement reads " + quoted + " — my own work answers it directly.",
        "**On method.** Your posting asks candidates to " + quoted + "; I build that way daily.",
    )
    assert cross_block_dupe_check(text) == []


def test_dupe_structural_blocks_excluded():
    """Letterhead / sign-off / salutation / Re line / date are formatting boilerplate -- duplication within them is not a defect."""
    text = _cl(
        "Your posting names accountability.",
        "I stayed with problems until they were measured, and I own the outcome.",
    )
    assert cross_block_dupe_check(text) == []


def test_dupe_short_document_no_crash():
    assert cross_block_dupe_check("Just one paragraph, nothing to compare.") == []


# ── claim-verification (incident 4) ────────────────────────────────────────

_JD_TEMPLATE = """# JD.md

## Submission Checklist

{checklist}

## JD Text (Original)

{body}
"""


def _jd(checklist: str, body: str) -> str:
    return _JD_TEMPLATE.format(checklist=checklist, body=body)


def test_claim_flags_untraceable_entity():
    """Checklist action entity with no basis in the JD body -> must hit (incident 4 mechanism)."""
    text = _jd(
        "- [ ] ⚠️ Book and pass the **FooBar Academy online technical assessment**, submit results with application",
        "The team builds AI workflows for agencies. No assessment is mentioned here.",
    )
    hits = claim_verification_check(text)
    assert hits and "FooBar Academy" in hits[0][1]


def test_claim_alias_normalization_no_false_positive():
    """Positive control (pkg21 evidence): checklist abbreviation vs JD full name -- normalised, no false positive."""
    text = _jd(
        "- [ ] ⚠️ Book and pass the **CivicTech Digital Academy online technical assessment**, submit results with application",
        "Pass the online technical assessment administered by the Civic Technology "
        "Agency’s Digital Academy and submit the official results to Contoso.",
    )
    assert claim_verification_check(text) == []


def test_claim_internal_process_items_not_flagged():
    """Internal-process items (files in this directory / write-back to ledger / record time) do not apply JD tracing -- no false positive."""
    text = _jd(
        "- [ ] Use this directory's `Resume_[CANDIDATE].md` (customised Profile)\n"
        "- [ ] Use this directory's `CoverLetter.md`\n"
        "- [ ] Submit via the original link (do not use agencies / aggregators)\n"
        "- [ ] After submitting, write the status back to `docs/JD_master_ledger.xlsx` (via `docs/jd_registry_ops.py`)\n"
        "- [ ] Record the submission time at the top of this document",
        "The team builds AI workflows for agencies.",
    )
    assert claim_verification_check(text) == []


def test_claim_non_jd_document_not_applicable():
    """Non-JD.md (no two sections) -> rule not applicable, returns [] (CL/resume no false positive)."""
    assert claim_verification_check(_cl("A plain cover letter body block.")) == []


# ── cl-resume-consistency (the original fifth rule from the proposal) ────────────────

@pytest.mark.parametrize("phrase,expect_frag", [
    ("I have spent eleven years in technology — all of it in AI", "eleven years in technology"),
    ("AI programme leader with 11 years in technology", "11 years in technology"),
    ("a leader with fifteen years in AI", "fifteen years in AI"),
    ("8+ years in AI/ML", "8+ years in AI/ML"),
    ("eight in AI/ML", "eight in AI/ML"),
    ("MEng (CE) from NBU", "MEng (CE"),
    ("Master of Engineering in Computer Engineering", "Master of Engineering in Computer Engineering"),
    ("Evergrande New Energy Vehicle (Tianjin)", "Evergrande New Energy Vehicle"),
    ("Bilingual English / Mandarin", "Bilingual English / Mandarin"),
])
def test_deprecated_phrasing_flagged(phrase, expect_frag):
    hits = cl_resume_consistency_check(f"Profile: {phrase}.")
    assert hits and expect_frag in hits[0][1], hits


@pytest.mark.parametrize("clean", [
    "whose work has been in AI since 2011",
    "in AI since 2011, classical machine learning first",
    "Tools: Python (11 years); PyTorch; Docker",           # tool tenure != total-tenure calibration
    "production cloud engineering on Baidu AI Cloud (3-4 years)",
    "Master of Engineering from NBU",
    "Singapore Citizen",
])
def test_current_phrasing_not_flagged(clean):
    assert cl_resume_consistency_check(f"Profile: {clean}.") == []


# ── Digit normalisation for registered quantity keys (D3 CLI first-run evidence) ────────

@pytest.mark.parametrize("word_form,digit_form", [
    ("ten percentage points", "10 percentage points"),
    ("seven person AI team", "7 person AI team"),
    ("sixteen granted patents", "16 granted patents"),
    ("two roles", "2 roles"),
])
def test_registered_quantity_word_and_digit_forms_equivalent(word_form, digit_form):
    """Word form and digit form are the same quantity -- after normalisation neither is flagged."""
    assert number_provenance_check(f"Verified: {word_form} in the record.") == []
    assert number_provenance_check(f"Verified: {digit_form} in the record.") == []


@pytest.mark.parametrize("word_form,digit_form", [
    ("forty-two models", "42 models"),          # unregistered: both spellings must be flagged
    ("seven procurement packages", "7 procurement packages"),  # incident 3 core
])
def test_unregistered_quantity_both_forms_flagged(word_form, digit_form):
    assert number_provenance_check(f"We delivered {word_form} last year.")
    assert number_provenance_check(f"We delivered {digit_form} last year.")


def test_accident_subject_still_flagged_after_canonicalization():
    """Incident 3 core (with compound modifiers) stays unregistered -- normalisation does not affect the regression anchor."""
    assert number_provenance_check("We ran full-cycle tendering across seven procurement packages.")
