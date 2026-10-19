"""Golden-set pytest (hard gate per the acceptance criteria: 4/4 accidents reproduced).

For every case in evals/golden/ (declared by manifest.json):
  before (accident-state version) — the rule must hit, and the hit segment
  must contain the specified anchor substring;
  after  (fixed/control version) — the rule must stay silent or not hit the
  specified anchor.
Assertion semantics are driven by the manifest's expect fields (min_hits/
max_hits/must_contain/must_not_contain); this file contains no hand-written
expectations — the expectation IS the golden-set declaration itself; changing
an expectation = changing the manifest (audit trail).

Golden-set provenance discipline (manifest.sources): git archaeology / live /
review-citation reconstruction; the reconstruction derivation rules are
documented in the scripts/01_build_golden_set.py comments. The files are
already scrubbed (direct contact identifiers) and safe to commit to git for
CI residency (part of the gate since the CI stage).

pytest invocation discipline (lesson learned): always run pytest from the
repo root (rootdir → pyproject → testpaths), otherwise the collection scope
runs wild.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.compliance.engine import KIND_CORPUS, Context, check_text
from apa.compliance.rules.accidents import Corpus, accident_rules, fork_of

# Golden corpus holds real incident data and is NOT shipped in the public
# repo (exposure discipline) — skip gracefully when absent.
GOLDEN_MANIFEST = Path(__file__).resolve().parents[2] / "evals" / "golden" / "manifest.json"
pytestmark = pytest.mark.skipif(
    not GOLDEN_MANIFEST.exists(),
    reason="golden corpus not present (public repo: synthetic rebuild pending)",
)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "evals" / "golden"
CORPUS_DIR = GOLDEN / "corpus"

_RULES = {r.id: r for r in accident_rules()}


def _load_manifest() -> dict:
    return json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))


def _run(rule_id: str, path: Path) -> list[str]:
    """Run a single rule; returns the list of hit segments (corpus-kind rules
    use the golden mini-corpus)."""
    rule = _RULES[rule_id]
    text = path.read_text(encoding="utf-8")
    if rule.kind == KIND_CORPUS:
        others = {
            f.name: f.read_text(encoding="utf-8")
            for f in sorted(CORPUS_DIR.glob("CoverLetter_*.md"))
            if f.resolve() != path.resolve()
        }
        ctx = Context(corpus=Corpus(others=others, target_fork=fork_of(path.name)))
        findings = check_text(text, [rule], ctx)
    else:
        findings = check_text(text, [rule])
    return [f.segment for f in findings]


def _check(rule_id: str, path: Path, expect: dict) -> None:
    segs = _run(rule_id, path)
    if "min_hits" in expect:
        assert len(segs) >= expect["min_hits"], (
            f"{path.name} rule {rule_id} should hit at least {expect['min_hits']} times, got {len(segs)}: {segs}")
    if "max_hits" in expect:
        assert len(segs) <= expect["max_hits"], (
            f"{path.name} rule {rule_id} should hit at most {expect['max_hits']} times, got {len(segs)}: {segs}")
    for sub in expect.get("must_contain", []):
        assert any(sub in s for s in segs), (
            f"{path.name} rule {rule_id} hit segments must contain '{sub}', got: {segs}")
    for sub in expect.get("must_not_contain", []):
        assert not any(sub in s for s in segs), (
            f"{path.name} rule {rule_id} must not hit '{sub}', got: {segs}")


# ── golden-set structure self-check (foolproofing: cases/files/corpus complete) ──

def test_golden_structure_complete():
    m = _load_manifest()
    assert m["version"] == "v0"
    assert len(m["cases"]) == 6, "v0 = 4 numbered accidents (accident 2 in two states) + 1 pre-existing rule from the planning doc"
    for case in m["cases"]:
        for side in ("before", "after"):
            ref = case[side]
            p = GOLDEN / case["dir"] / ref["file"]
            assert p.is_file(), f"golden file missing: {p}"
            assert ref["source"] in {"git", "live", "review-citation"}, ref
            assert ref.get("detail"), f"{case['id']} {side} missing source detail"
        # the version pair must genuinely differ (before ≠ after), otherwise
        # it is not a valid version pair
        b = (GOLDEN / case["dir"] / case["before"]["file"]).read_text(encoding="utf-8")
        a = (GOLDEN / case["dir"] / case["after"]["file"]).read_text(encoding="utf-8")
        assert b != a, f"{case['id']}: before and after have identical content, not a valid version pair"
    assert CORPUS_DIR.is_dir() and len(list(CORPUS_DIR.glob("CoverLetter_*.md"))) >= 2


def test_golden_files_scrubbed():
    """PII discipline: golden documents must not contain direct contact
    identifiers."""
    banned = ["158-2213", "dusijun@gmail", "StevenLOL", "140 Hill",
              "Steven Du", "STEVEN DU", "STEVEN-DU"]
    for p in sorted(GOLDEN.rglob("*.md")):
        t = p.read_text(encoding="utf-8")
        for b in banned:
            assert b not in t, f"{p.name} residual PII: {b}"


# ── per-case reproduction (hard gate per the acceptance criteria) ──────────

def _cases() -> list:
    """Lazily collect golden cases (when the public repo has no corpus,
    produce a skip placeholder — collection must not crash)."""
    if not GOLDEN_MANIFEST.exists():
        return [pytest.param(None, id="golden-missing",
                             marks=pytest.mark.skip(reason="golden corpus not present (public repo)"))]
    return [pytest.param(c, id=c["id"]) for c in _load_manifest()["cases"]]


@pytest.mark.parametrize("case", _cases())
def test_accident_reproduction(case: dict):
    if case is None:  # golden-missing placeholder (the skip mark already
        # handles it; this is a double safety net)
        pytest.skip("golden corpus not present")
    for side in ("before", "after"):
        _check(case["rule"], GOLDEN / case["dir"] / case[side]["file"], case["expect"][side])
