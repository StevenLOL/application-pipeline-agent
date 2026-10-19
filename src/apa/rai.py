"""R4 counterfactual fairness testing (Responsible AI: bias testing).

How the JD requirement lands here: **paired inputs differing only in
irrelevant attributes (employer name, organisation class) must produce
invariant decisions** (hand-written Giskard-style paired tests, per docs/06).

Fairness surfaces under test:
1. **Rule engine**: the same cover letter with only employer/organisation
   names swapped -> the findings set must be invariant. If any incident rule
   regex happens to fire only on government (or only on private) wording,
   this test flips - a flip means the decision is driven by an irrelevant
   attribute, which is bias.
2. **Retrieval**: the same JD with a swapped employer -> top-k ranking must
   be stable (robustness of the semantic space to irrelevant entity swaps;
   requires the local embedding model, skipif absent).

Output: CounterfactualResult (invariance + flip records) -> rendered report
(reports/rai/). Counterfactual invariance is asserted **only on deterministic
components**; bias testing of LLM-generated content belongs to the assessment
layer (docs/18 layering).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CounterfactualResult:
    pair_id: str
    description: str
    output_a: Any
    output_b: Any
    invariant: bool
    diff: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "description": self.description,
            "invariant": self.invariant,
            "diff": self.diff,
        }


def _apply(text: str, substitutions: list[tuple[str, str]]) -> str:
    for old, new in substitutions:
        text = text.replace(old, new)
    return text


def run_counterfactual(
    base_text: str,
    substitutions: list[tuple[str, str]],
    judge: Callable[[str], Any],
    pair_id: str,
    description: str = "",
    compare: Callable[[Any, Any], bool] | None = None,
) -> CounterfactualResult:
    """A/B counterfactual: A = original; B = substituted. Decisions must match.

    ``compare`` defaults to exact output equality (strict invariance for
    structured outputs like findings lists / rankings); pass a custom
    comparator when semantic equivalence with different forms is acceptable.
    """
    text_a = base_text
    text_b = _apply(base_text, substitutions)
    out_a = judge(text_a)
    out_b = judge(text_b)
    cmp = compare or (lambda a, b: a == b)
    ok = cmp(out_a, out_b)
    diff = "" if ok else f"A={out_a!r:.200} B={out_b!r:.200}"
    return CounterfactualResult(pair_id, description, out_a, out_b, ok, diff)


def render_report(results: list[CounterfactualResult], title: str) -> str:
    lines = [f"# RAI counterfactual fairness report: {title}", "",
             f"- pairs: {len(results)}",
             f"- invariance passed: **{sum(1 for r in results if r.invariant)}/{len(results)}**",
             "",
             "| pair | description | verdict | diff |",
             "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.pair_id} | {r.description} | "
                     f"{'PASS invariant' if r.invariant else 'FAIL flipped'} | {r.diff[:80] or '-'} |")
    lines += [
        "",
        "> Counterfactual invariance is asserted on deterministic components only;",
        "> bias testing of LLM-generated content belongs to the assessment layer",
        "> (docs/18 layering). A flip means the decision is driven by an irrelevant",
        "> attribute = bias; it must be attributed to the specific rule or feature.",
    ]
    return "\n".join(lines)
