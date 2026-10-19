"""D6+ acceptance: the reviewer's behaviour on review documents (synthetic).

Self-audit discipline: the reviewer must be able to audit its own kind of
output. If it catches nothing in an APA-style review, either the document
is perfect or the reviewer is blind — this test requires it to run, write
a report, and reach a legal verdict. Exact hit counts stay out of the
assertions (documents evolve; no overfitting to counts).

The synthetic fixtures live in ``review_fixtures/`` and ship with the repo,
so this test runs everywhere (no skip). Real workspace documents can be
audited too via ``APA_WORKSPACE_DOCS`` — an env var, never a hardcoded
private path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from apa.review.audit import audit_file, render_markdown

FIX = Path(__file__).parent / "review_fixtures"
SELF = FIX / "self_review.md"
THIRD = FIX / "third_party_review.md"

ALLOWED_ADVICE = {"ACCEPT", "REVIEW", "EVIDENCE-REQUEST", "REJECT"}
ALLOWED_SEVERITY = {"high", "medium", "low"}


@pytest.mark.parametrize("doc", [SELF, THIRD], ids=["apa-self", "third-party"])
def test_audit_synthetic_documents(doc: Path) -> None:
    """Synthetic reviews are auditable: legal structure, legal verdict,
    renderable report."""
    res = audit_file(doc)
    assert len(res.blocks) > 10
    d = res.to_dict()
    assert set(d["counts"]) <= ALLOWED_SEVERITY
    for f in d["findings"]:
        assert f["severity"] in ALLOWED_SEVERITY
        assert f["dimension"] in {
            "evidence", "repro", "wording", "grade", "action",
            "boundary", "numeric", "provenance",
        }
        assert f["advice"] in ALLOWED_ADVICE
        assert f["line"] >= 1
    assert res.verdict().startswith(("ACCEPT", "REJECT-OR-REVISE"))
    md = render_markdown(res)
    assert "Review Comment Audit Report" in md and res.verdict() in md


def test_self_audit_finds_something() -> None:
    """Self-audit effectiveness: the reviewer must not be completely blind
    to an APA-style review (nor drown it in noise)."""
    res = audit_file(SELF)
    assert res.findings, "zero findings on the self review -> self-audit is suspect"
    assert len(res.findings) < max(20, len(res.blocks) // 2), (
        "too many findings -> the reviewer manufactures noise")


def test_no_boundary_false_positive_on_prohibitions() -> None:
    """Negative-context exemption: the fixture's iron-rule prohibitions
    ("never pass Chinese parameters through shell strings", "Never bypass HITL") are prohibitions, not
    boundary-violating advice — flagging them punishes the disciplined."""
    res = audit_file(SELF)
    assert not [f for f in res.findings if f.dimension == "boundary"], (
        "prohibitive wording misread as boundary-violating advice "
        "(negative-context exemption broken)")


def test_third_party_unanchored_claim_gets_evidence_request() -> None:
    """The unanchored boast in the third-party fixture must be caught."""
    res = audit_file(THIRD)
    assert any(f.dimension == "evidence" for f in res.findings), (
        "an unanchored claim slipped through the evidence dimension")


def test_cli_writes_report_for_synthetic_doc(tmp_path) -> None:
    from apa.cli import main

    out = tmp_path / "r.md"
    js = tmp_path / "r.json"
    assert main(["review", "audit", str(THIRD), "--out", str(out),
                 "--json", str(js)]) == 0
    assert "Review Comment Audit Report" in out.read_text(encoding="utf-8")
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["blocks"] > 10 and data["verdict"]


# ---- optional: real workspace documents, env-gated (never hardcoded) ----

def _workspace_docs() -> list[Path]:
    root = os.environ.get("APA_WORKSPACE_DOCS", "").strip()
    if not root:
        return []
    p = Path(root)
    if not p.is_dir():
        return []
    return sorted(p.glob("*.md"))


@pytest.mark.skipif(not _workspace_docs(),
                    reason="APA_WORKSPACE_DOCS not set or empty")
@pytest.mark.parametrize("doc", _workspace_docs(),
                         ids=lambda p: p.name)
def test_audit_workspace_documents(doc: Path) -> None:
    """Real documents stay auditable (opt-in via APA_WORKSPACE_DOCS)."""
    res = audit_file(doc)
    assert len(res.blocks) > 5
    assert res.verdict().startswith(("ACCEPT", "REJECT-OR-REVISE"))
