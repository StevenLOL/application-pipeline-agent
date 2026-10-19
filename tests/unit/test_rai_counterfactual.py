"""R4: counterfactual fairness - employer/organisation invariance of the
rule engine and retrieval.

Target 1 (core, offline): **rule engine** - the same real cover letter
(PII-scrubbed golden corpus) with only employer/organisation names swapped
-> the findings set must be invariant. If an incident rule regex happens to
fire only on government (or private) wording, this flips.

Target 2 (skipif local embedding model absent): **retrieval** - top-k stable
under employer swaps (semantic-space robustness to irrelevant entities).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apa.compliance.engine import check_text
from apa.compliance.rules import get_rules
from apa.rai import render_report, run_counterfactual

GOLDEN = Path(__file__).resolve().parents[2] / "evals" / "golden"
CORPUS = GOLDEN / "corpus"
BASE_CL = CORPUS / "CoverLetter_ACME_AI_Data_Office_EN.md"

pytestmark = pytest.mark.skipif(
    not BASE_CL.exists(),
    reason="golden corpus not present (public repo: synthetic rebuild pending)",
)


def _findings(text: str) -> list[tuple]:
    """Rule-engine verdict -> sorted (code, hard, line) tuples."""
    findings = check_text(text, get_rules("all"))
    return sorted((f.code, f.hard, f.line) for f in findings)


# Counterfactual pairs: organisation class and employer names
# (attributes irrelevant to compliance verdicts).

PAIR_GOV_TO_PRIVATE = [
    ("Ministry of Manpower", "a global technology company"),
    ("Careers@Gov", "the company careers portal"),
    ("GOV-SYNTH00003", "REQ-2026-001"),
    ("public organisation", "large organisation"),
    ("ministry", "organisation"),
]

PAIR_EMPLOYER_HISTORY = [
    ("Zephyr", "an automaker"),
    ("ZTE", "a telecom equipment vendor"),
    ("Evergrande", "a property group"),
]


def test_rules_invariant_to_employer_class_swap():
    """Organisation-class swap (gov <-> private): findings set invariant."""
    text = BASE_CL.read_text(encoding="utf-8")
    result = run_counterfactual(
        text, PAIR_GOV_TO_PRIVATE, _findings,
        pair_id="gov-to-private",
        description="organisation class: government -> private",
    )
    assert result.invariant, (
        f"rule engine driven by organisation class (bias): {result.diff}"
    )


def test_rules_invariant_to_employer_history_swap():
    """Career-employer name swap (real companies -> generic): invariant."""
    text = BASE_CL.read_text(encoding="utf-8")
    result = run_counterfactual(
        text, PAIR_EMPLOYER_HISTORY, _findings,
        pair_id="employer-history",
        description="career employer names -> generic",
    )
    assert result.invariant, f"rule engine driven by employer names (bias): {result.diff}"


def test_report_rendered(tmp_path):
    """Report rendering (rai artifact; batch runs land in reports/rai/)."""
    text = BASE_CL.read_text(encoding="utf-8")
    results = [
        run_counterfactual(text, PAIR_GOV_TO_PRIVATE, _findings,
                           "gov-to-private", "organisation class swap"),
        run_counterfactual(text, PAIR_EMPLOYER_HISTORY, _findings,
                           "employer-history", "employer name swap"),
    ]
    md = render_report(results, "rule-engine employer invariance")
    assert "counterfactual fairness report" in md and "invariance passed" in md


def test_retrieval_invariant_to_employer_swap():
    bge = Path(r"E:\dev\models\bge-small-en-v1.5\model.safetensors")
    if not bge.exists():
        pytest.skip("bge-small-en-v1.5 not present (local model, rule 12)")
    from apa.retrieve import BgeEmbedder, Retriever

    r = Retriever(BgeEmbedder(bge.parent), GOLDEN / "corpus" / "_retrieval_idx")

    def top_ids(text: str) -> list[str]:
        return [h.doc_id for h in r.search(text, k=3)]

    jd = ("Senior AI Engineer. RAG pipelines, agentic workflows. "
          "Closing on 22 Oct 2026. Employer: Ministry of Manpower.")
    result = run_counterfactual(
        jd, [("Ministry of Manpower", "a global technology company")],
        top_ids, pair_id="retrieval-employer-swap",
        description="retrieval top-k stability under employer swap",
    )
    assert result.invariant, (
        f"retrieval ranking driven by employer name (bias): {result.diff} - "
        "semantic retrieval should rank by content similarity, not employer entity"
    )
