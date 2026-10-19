"""D2 rule unit tests: engine dispatch + template-residue (incident 1) + number-provenance (incident 3).

The residue rule is verified with an **injected negative**: inject the sentence
unique to the ACME letter into a copy of the Helios letter, and the rule must
hit — this reproduces the mechanism of incident 1 rather than faking a
"historical file" (golden-standard version pairs belong to D3).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apa.compliance.engine import (
    KIND_LAMBDA, KIND_MUST_HAVE, KIND_REGEX, Context, Rule, check_files, check_text,
)
from apa.compliance.rules.accidents import (
    Corpus, accident_rules, fork_of, number_provenance_check, template_residue_check,
)
from apa.compliance.rules.inherited_130 import inherited_rules

SINGAPORE = Path(r"<private-workspace>")
ME = SINGAPORE / "me"
_Helios = ME / "CoverLetter_Fabrikam_AI_Transformation_Helios_EN.md"
_ACME = ME / "CoverLetter_ACME_AI_Data_Office_EN.md"

needs_singapore = pytest.mark.skipif(not _Helios.is_file(), reason="singapore 工作区不在本机")

# Prototype residue sentence of incident 1 (unique to the ACME letter: ministry data-domain phrasing)
_ACME_RESIDUE = ("In a ministry the data already exists — workforce and employment data, "
                "workplace safety and health records, foreign-manpower administration")


# ── engine dispatch ────────────────────────────────────────────────────

def test_engine_rule_kinds_and_severity():
    text = "alpha beta gamma"
    rules = [
        Rule(id="t-regex", code="T", desc="找 beta", kind=KIND_REGEX, hard=True, pattern=r"beta"),
        Rule(id="t-must", code="T", desc="必须含 delta", kind=KIND_MUST_HAVE, hard=True, must=r"delta"),
        Rule(id="t-lambda", code="T", desc="lambda 找 gamma", kind=KIND_LAMBDA, hard=False,
             fn=lambda t: [(1, "gamma")] if "gamma" in t else []),
    ]
    findings = check_text(text, rules)
    by_id = {f.rule_id: f for f in findings}
    assert by_id["t-regex"].hard and by_id["t-regex"].line == 1
    assert by_id["t-must"].hard and by_id["t-must"].line == 0
    assert not by_id["t-lambda"].hard


def test_engine_missing_file_and_counts():
    report = check_files([Path("/nonexistent/x.md")], inherited_rules()[:1])
    fr = report.files[0]
    assert fr.missing and "MISSING" in report.render_text()


def test_llm_rule_without_client_raises_not_silent():
    """An llm-kind rule registered without an injected client -> explicit error, never a silent skip (pre-D3 discipline)."""
    from apa.compliance.engine import KIND_LLM

    rule = Rule(id="t-llm", code="T", desc="llm 规则", kind=KIND_LLM, hard=True,
                fn=lambda t, llm: [])
    with pytest.raises(RuntimeError, match="no LLM client was injected"):
        check_text("anything", [rule])


def test_engine_report_json_roundtrip():
    rep = check_files([_Helios], inherited_rules()) if _Helios.is_file() else None
    if rep is None:
        pytest.skip("singapore 不在本机")
    d = rep.to_dict()
    assert d["hard_count"] == sum(1 for f in d["files"] for x in f["findings"] if x["hard"])
    assert "findings" in d["files"][0]


# ── template-residue (incident 1) ──────────────────────────────────────

def test_fork_of():
    assert fork_of("CoverLetter_ACME_AI_Data_Office_EN.md") == "ACME"
    assert fork_of("CoverLetter_Northwind_AI_Solution_Architect_EN.md") == "Northwind"
    assert fork_of("简历.md") is None


def test_residue_detects_injected_fork_sentence():
    """Injecting the ACME-unique sentence into the Helios letter -> must hit (incident-1 mechanism replay).

    The corpus uses the full set of CLs under me/ (real usage shape): header
    blocks / shared achievements common to multiple forks are never judged
    unique; assertions are targeted hits (residue present / absent), and no
    zero-hit assertion is made on a clean letter — a shared achievement block
    may legitimately trigger an alert, left for human review (alert-level role).
    """
    if not (_Helios.is_file() and _ACME.is_file()):
        pytest.skip("singapore 不在本机")
    helios = _Helios.read_text(encoding="utf-8")
    others = {
        f.name: f.read_text(encoding="utf-8")
        for f in sorted(ME.glob("CoverLetter_*.md"))
        if f.resolve() != _Helios.resolve()
    }
    corpus = Corpus(others=others, target_fork="Fabrikam")

    clean_segs = " ".join(seg for _, seg in template_residue_check(helios, corpus))
    assert "workforce and employment data" not in clean_segs, "干净信不应命中 ACME 残留句"

    injected = helios.replace(
        "I would welcome the opportunity to discuss",
        _ACME_RESIDUE + "\n\nI would welcome the opportunity to discuss",
    )
    hits = template_residue_check(injected, corpus)
    hit_segs = " ".join(seg for _, seg in hits)
    assert "workforce and employment data" in hit_segs, "注入的 ACME 残留句必须被检出"
    assert any("[ACME 独有内容]" in seg for _, seg in hits)
    assert all(line > 0 for line, _ in hits)


def test_residue_ignores_multifork_shared_achievement():
    """A real achievement shared across multiple forks (ten percentage points) is not residue — no false positive."""
    shared = "speech-recognition accuracy improved by ten percentage points across mass-produced vehicles"
    others = {
        "CoverLetter_ACME_AI_Data_Office_EN.md": f"At Zephyr {shared}.",
        "CoverLetter_Woodgrove_TRO_Transport_AI_Programme_EN.md": f"My work: {shared}.",
    }
    corpus = Corpus(others=others, target_fork="Fabrikam")
    hits = template_residue_check(f"I improved things: {shared}.", corpus)
    assert hits == [], "多 fork 共有内容不应被判为残留"


def test_residue_skips_non_cl_target():
    corpus = Corpus(others={"CoverLetter_ACME_AI_Data_Office_EN.md": "workforce data"}, target_fork=None)
    assert template_residue_check("workforce data", corpus) == []


# ── number-provenance (incident 3) ─────────────────────────────────────

# D3 update: "165 tests" is now registered (2026-10-17 live run of ragdemo:
# 165 items = 164 passed + 1 skipped, the real asset pack tome-1.7.6-gfx was
# missing; docs/132:4) — moved from "to be confirmed" to the registered list.
# The incident-3 body "seven procurement packages" stays unregistered
# (golden-standard negative).
@pytest.mark.parametrize("claim", ["seven procurement packages", "forty-two models"])
def test_unregistered_quantity_flagged(claim):
    hits = number_provenance_check(f"We delivered {claim} last year.")
    assert hits, f"未登记出处的 {claim} 必须被 flag"
    assert "needs manual reconciliation" in hits[0][1]


@pytest.mark.parametrize("claim", ["sixteen granted patents", "59/63", "31/31", "39/63",
                                   "ten percentage points", "eleven years in technology",
                                   "165 tests", "61 tests", "three granted vision patents"])
def test_registered_quantity_not_flagged(claim):
    assert number_provenance_check(f"Verified: {claim} in the record.") == []
