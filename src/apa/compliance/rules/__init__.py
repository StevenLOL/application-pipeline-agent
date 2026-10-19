"""F1 rule catalog: inherited set (_130's R1–R12) + accident set (A1–A5) + F10 queue set + format set (FMT).

Selectors: --rules all | inherited | accidents | f10 | format
  inherited = run only inherited rules (behavior-equivalent to the production singapore _130, for regression comparison)
  accidents = run only accident-driven rules (warning level)
  f10       = run only submission-queue rules (deadline-buffer/portal-health-gate/channel-divergence,
              applied to data/submission_queue.json; non-queue files are automatically not applicable)
  format    = run only SG format requirements (CL structure four-piece set / resume header / reverse
              chronological order / no tables / British spelling; all warning level, sourced from the
              singapore master conventions + measured delivery-package structure)
  all       = merge of all four sets (use this tier for formal checks)

Discipline for adding rules (staged development plan, section 8): rules come only from real accidents or
existing conventions, never invented; inherited rules must not be modified (recorded verbatim), new
semantics always go through new rule ids / new families (the format family follows exactly this path).
"""

from __future__ import annotations

from ..engine import Rule
from . import accidents, f10 as f10_rules_mod, format as format_rules_mod, inherited_130

_SETS = {
    "inherited": inherited_130.inherited_rules,
    "accidents": accidents.accident_rules,
    "f10": f10_rules_mod.f10_rules,
    "format": format_rules_mod.format_rules,
}


def get_rules(name: str = "all") -> list[Rule]:
    if name == "all":
        return (inherited_130.inherited_rules() + accidents.accident_rules()
                + f10_rules_mod.f10_rules() + format_rules_mod.format_rules())
    if name in _SETS:
        return _SETS[name]()
    raise ValueError(f"unknown rule set: {name} (choose from all/inherited/accidents/f10/format)")
