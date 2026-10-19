"""D6+ / Meta-governance: review-comment auditor unit tests.

Two disciplines apply equally to the auditor itself (an isomorphic application
of the D3 false-positive-rate lesson):
- **No misses:** opinions without evidence / out of scope / without a graded
  argument must be caught.
- **No over-sensitivity:** a well-evidenced, high-quality opinion must yield
  **zero hits** (otherwise the auditor itself generates noise).
- **No punishment of honesty:** an author who labelled a point "to be verified /
  assumed / defined" is not treated as evidence-less.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.review.audit import (
    audit_file,
    check_actionability,
    check_boundary,
    check_evidence,
    check_grade,
    check_numeric_consistency,
    check_provenance,
    check_reproducibility,
    check_wording,
    parse_blocks,
    render_markdown,
)

# ---- A "bad" review comment (should be caught) ----

BAD = """# Review Comments

- This architecture has problems, it should be refactored
- Suggest directly editing the ledger xlsx to improve efficiency
- All modules must be rewritten
- 🔴 Performance is insufficient
"""

# ---- A "good" review comment (should yield zero hits) ----

GOOD = """# Review Comments

> Review date: 2026-10-18 | Reviewer: Rune | Subject: `docs/08_ProductDesign_v1.0.md`
> Sources: `reports/evals/run_001/`, `src/apa/compliance/engine.py:42`

## Gaps

- 🔴 Sparse deadline column affects submission decisions: measured 54/756 rows have values; suggest filling via `jd_registry_ops.py` update
- 🟡 Drift in verdict vocabulary will break statistics: 9 distinct values; suggest collapsing to an enum and re-running `pytest tests/unit`
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---- Per-dimension: bad samples must hit ----

def test_evidence_flags_unsubstantiated_claims() -> None:
    blocks = parse_blocks(BAD)
    hits = check_evidence(blocks)
    assert hits, "Evidence-less items must be caught"
    assert all(h.severity == "high" and h.advice == "EVIDENCE-REQUEST" for h in hits)


def test_boundary_is_reject_level() -> None:
    hits = check_boundary(parse_blocks(BAD), BAD)
    assert hits and hits[0].severity == "high" and hits[0].advice == "REJECT"
    assert "directly editing the ledger" in hits[0].note or "directly editing the ledger" in hits[0].block


def test_wording_flags_absolute_without_support() -> None:
    hits = check_wording(parse_blocks(BAD))
    assert any(h.dimension == "wording" for h in hits)


def test_grade_requires_impact_chain() -> None:
    hits = check_grade(parse_blocks(BAD))
    assert hits, "A 🔴 marker with no impact-chain argument must be caught"


def test_provenance_missing_all() -> None:
    hits = check_provenance(BAD)
    names = {h.block for h in hits}
    assert any("Reviewer" in n for n in names)
    assert any("date" in n for n in names)


# ---- Good sample: must yield zero hits (guard against over-sensitivity) ----

def test_good_review_has_no_findings(tmp_path) -> None:
    p = _write(tmp_path, "good.md", GOOD)
    res = audit_file(p)
    assert not res.findings, f"High-quality opinion should yield zero hits; actual: {[f.to_dict() for f in res.findings]}"
    assert res.verdict() == "ACCEPT (evidence, action, and impact chain all present)"


# ---- Anti-false-positive exemptions ----

def test_honest_marking_exempts_evidence_check() -> None:
    text = "- Suggest migrating to the new approach (to be verified: confirm dependency version compatibility first)"
    assert not check_evidence(parse_blocks(text)), "Labelled as to-be-verified -> evidence dimension exempt"


def test_heading_inherits_section_evidence() -> None:
    """A heading itself has no anchor, but the subsection has evidence -> exempt
    (false positive from separating heading from body)."""
    text = (
        "### 1.3 Two content-level defects\n"
        "- Package 22 JD.md's Digital Academy action item is never mentioned in the JD text (measured 0 hits)\n"
    )
    blocks = parse_blocks(text)
    assert blocks[0].section_evidence is True
    assert not check_evidence(blocks)


def test_heading_without_section_evidence_is_flagged() -> None:
    """A heading makes a claim but the whole section has no evidence -> must be
    caught (guard against misses)."""
    text = "### 1.4 This module has serious defects\n- Needs refactoring\n"
    blocks = parse_blocks(text)
    assert blocks[0].section_evidence is False
    assert any(h.line == 1 for h in check_evidence(blocks))


def test_negation_context_exempts_boundary() -> None:
    """Negation context exempt: 'ban passing Chinese text directly via shell' is
    a prohibition, not a suggestion (isomorphic to D2 A1 negation context)."""
    text = "- Write channel: via jd_registry_ops.py (conda base python, **ban passing Chinese text directly via shell**, iron rule 3)"
    assert not check_boundary(parse_blocks(text), text)
    # but a genuine out-of-scope suggestion must be caught
    assert check_boundary(parse_blocks("- Suggest directly editing the ledger xlsx"), "- Suggest directly editing the ledger xlsx")


def test_cjk_quote_counts_as_evidence() -> None:
    """Chinese quotation marks 「」 are also evidence anchors (quotes)."""
    text = "- Suggest collapsing to an enum (current state mixes three spellings: 「delivered, delivered-and-submittable (pending user ruling)」)"
    assert not check_evidence(parse_blocks(text))
    # short quote (<8 chars) is not an evidence anchor: too short to support a claim
    short = "- Suggest collapsing to an enum ('delivered' mixed badly)"
    assert check_evidence(parse_blocks(short))


def test_background_sentence_not_required_to_have_evidence() -> None:
    """Background sentences carry no claim/action -> not required to have an
    evidence anchor (guard against over-sensitivity)."""
    text = "- APA F10 affected list: determination depends on 'deadline in failure window'; without a deadline it cannot be determined"
    assert not check_evidence(parse_blocks(text))


def test_evidence_label_line_is_exempt() -> None:
    """A '- **Evidence**: ...' line is itself an argument and should not be
    required to carry its own anchor (docs/04 measured false positive)."""
    text = "- **Evidence**: point estimates being untrustworthy at small sample sizes is statistical common sense; before/after paired comparisons should use the Wilcoxon test"
    assert not check_evidence(parse_blocks(text))


def test_bullet_with_section_evidence_is_downgraded() -> None:
    """This line has no anchor but the same subsection has evidence -> downgrade
    to medium/REVIEW, do not flag as 'unsubstantiated'."""
    text = (
        "### G2 Evaluation division of labour\n"
        "- **Evidence**: Ragas natively provides `context_precision` (measured against 2 metrics)\n"
        "- **Suggestion**: DeepEval as the CI gate, Ragas as the R1 metric\n"
    )
    blocks = parse_blocks(text)
    hits = check_evidence(blocks)
    assert len(hits) == 1 and hits[0].severity == "medium" and hits[0].advice == "REVIEW"


def test_parent_heading_inherits_from_subsection() -> None:
    """A parent section's evidence inheritance is not stolen by a subsection
    heading (docs/04 measured: `## Three` immediately followed by `### G1`)."""
    text = "## Three, Gaps\n### G1 Observability\n- Evidence at `engine.py:42`\n"
    blocks = parse_blocks(text)
    assert blocks[0].section_evidence, "Parent heading should inherit subsection evidence"
    assert not [h for h in check_evidence(blocks) if h.line == 1]


def test_numeric_diff_declared_is_not_flagged() -> None:
    text = "- Ledger row count: 756 rows (calibration note: previously recorded as 748 rows; difference comes from two newly added CAG/SIA rows)"
    assert not check_numeric_consistency(text)


def test_numeric_diff_undeclared_is_flagged() -> None:
    text = "- Ledger row count: 756 rows\n- Ledger row count: 748 rows\n"
    hits = check_numeric_consistency(text)
    assert hits and hits[0].advice == "REVIEW"


# ---- Reproducibility / actionability ----

def test_repro_flags_url_only_evidence() -> None:
    text = "- Suggest refactoring based on https://example.com/some-claim"
    hits = check_reproducibility(parse_blocks(text))
    assert hits and hits[0].advice == "REVIEW"


def test_repro_passes_when_command_given() -> None:
    text = "- Suggest refactoring based on re-running `pytest tests/unit`"
    assert not check_reproducibility(parse_blocks(text))


def test_actionability_requires_channel() -> None:
    text = "- Suggest filling in the deadline field"
    assert check_actionability(parse_blocks(text))
    assert not check_actionability(
        parse_blocks("- Suggest filling in the deadline via `jd_registry_ops.py` update")
    )


# ---- Report and exit-code semantics ----

def test_audit_report_structure(tmp_path) -> None:
    p = _write(tmp_path, "bad.md", BAD)
    res = audit_file(p)
    md = render_markdown(res)
    assert "Conclusion" in md and "findings" in md
    d = res.to_dict()
    assert d["blocks"] == len(res.blocks)
    assert set(d["counts"]) <= {"high", "medium", "low"}
    for f in d["findings"]:
        assert f["advice"] in {"ACCEPT", "REVIEW", "EVIDENCE-REQUEST", "REJECT"}


def test_verdict_thresholds(tmp_path) -> None:
    bad = audit_file(_write(tmp_path, "bad.md", BAD))
    assert bad.verdict().startswith("REJECT-OR-REVISE")  # contains out-of-scope suggestion
    good = audit_file(_write(tmp_path, "good.md", GOOD))
    assert good.verdict().startswith("ACCEPT")


def test_verdict_evidence_high_is_not_rejection(tmp_path) -> None:
    """Only out-of-scope suggestions are rejected; an evidence-less claim ->
    supply evidence, not reject the whole opinion (docs/04 calibration)."""
    p = _write(tmp_path, "m.md", "## Conclusion\n- This module is poorly designed, it should be refactored\n")
    res = audit_file(p)
    assert any(f.severity == "high" and f.dimension == "evidence" for f in res.findings)
    assert res.verdict().startswith("ACCEPT-WITH-REQUESTS")


def test_cli_audit_exit_codes(tmp_path, monkeypatch, capsys) -> None:
    from apa.cli import main

    bad = _write(tmp_path, "bad.md", BAD)
    good = _write(tmp_path, "good.md", GOOD)
    assert main(["review", "audit", str(bad), "--fail-on", "high"]) == 1
    assert main(["review", "audit", str(good), "--fail-on", "high"]) == 0
    # report-only mode: even with high-severity findings, exit 0
    assert main(["review", "audit", str(bad)]) == 0


def test_cli_audit_writes_outputs(tmp_path) -> None:
    from apa.cli import main

    bad = _write(tmp_path, "bad.md", BAD)
    out = tmp_path / "report.md"
    js = tmp_path / "report.json"
    assert main(["review", "audit", str(bad), "--out", str(out), "--json", str(js)]) == 0
    assert out.exists() and "Review Comment Audit Report" in out.read_text(encoding="utf-8")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["findings"] and data["verdict"].startswith("REJECT")
