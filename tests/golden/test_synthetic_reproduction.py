"""Synthetic golden corpus (public repo): regression over the SHAPES of all
five incident families, fully synthetic employers and text - no real data.

Mirrors the private repo's tests/golden logic, but reads
evals/golden-synthetic/manifest.json and uses only rule families that ship in
the public repo (A1/A2/A3/A4/A5 - the accident family).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.compliance.engine import check_text
from apa.compliance.rules.accidents import (
    claim_verification_check,
    cl_resume_consistency_check,
    cross_block_dupe_check,
    number_provenance_check,
)

ROOT = Path(__file__).resolve().parents[2]
SYN = ROOT / "evals" / "golden-synthetic"

pytestmark = pytest.mark.skipif(
    not (SYN / "manifest.json").exists(),
    reason="synthetic golden corpus not present",
)


def _cases() -> list:
    manifest = json.loads((SYN / "manifest.json").read_text(encoding="utf-8"))
    return [pytest.param(c, id=c["id"]) for c in manifest["cases"]]


def _run_rule(rule: str, path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    runners = {
        "template-residue": None,  # A1 needs corpus context - handled separately
        "cross-block-dupe": cross_block_dupe_check,
        "number-provenance": number_provenance_check,
        "claim-verification": claim_verification_check,
        "cl-resume-consistency": cl_resume_consistency_check,
    }
    fn = runners[rule]
    if fn is None:
        return _run_a1(path)
    return fn(text)


def _run_a1(path: Path) -> list[tuple[int, str]]:
    """A1 needs the sibling corpus for exclusive-shingle computation."""
    from apa.compliance.rules.accidents import Corpus, template_residue_check

    others = {
        f.name: f.read_text(encoding="utf-8")
        for f in sorted((SYN / "corpus").glob("CoverLetter_*.md"))
    }
    fork = path.stem.replace("CoverLetter_", "").split("_")[0]
    return template_residue_check(path.read_text(encoding="utf-8"),
                                  Corpus(others=others, target_fork=fork))


def _check(rule: str, path: Path, expect: dict) -> None:
    hits = _run_rule(rule, path)
    min_hits = expect.get("min_hits")
    max_hits = expect.get("max_hits")
    if min_hits is not None:
        assert len(hits) >= min_hits, f"{path.name}: expected >= {min_hits}, got {len(hits)}: {hits}"
    if max_hits is not None:
        assert len(hits) <= max_hits, f"{path.name}: expected <= {max_hits}, got {len(hits)}: {hits}"


@pytest.mark.parametrize("case", _cases())
def test_synthetic_reproduction(case: dict):
    d = SYN / case["dir"]
    _check(case["rule"], d / case["before"]["file"], case["expect"]["before"])
    _check(case["rule"], d / case["after"]["file"], case["expect"]["after"])
