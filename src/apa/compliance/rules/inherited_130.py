"""F1 rule set, part 1: the 12 red lines inherited from singapore's `_130_cl_redline_check.py` (130 Phase 1).

**Record-verbatim discipline (2026-10-17)**: this file is a verbatim transcription of
`<private-workspace>`
— regexes, lambdas, thresholds, and hard/warn classifications are all unchanged; each rule notes its
source-file line number.
Modifying an inherited rule = breaking comparability with the production script; new semantics must go
through new rules (see accidents.py) rather than being changed here.
Version: 130 Phase 1 (finalized 2026-10-16, before package 22's first submission).
"""

from __future__ import annotations

from ..engine import (
    KIND_CONDITIONAL, KIND_LAMBDA, KIND_MUST_HAVE, KIND_REGEX, Rule,
)
import re

_SRC = "singapore/docs/_130_cl_redline_check.py"


def inherited_rules() -> list[Rule]:
    """Return a rule table behavior-equivalent to the production _130 (order also recorded verbatim, so reports stay comparable)."""
    rules: list[Rule] = []

    # ── FAIL_RULES (source file L28-42, 13 rules; order recorded verbatim) ──────────────────────
    fail_rules = [
        ("R11-1", "R11", 'prohibited: bare tenure like "5 years PM"',
         r"\b(5|6|4\.2|6\.3)\s*(years?|年)\s*(of\s*)?(PM|product management|Product Manager)\b", True, 29),
        ("R11-2", "R11", 'prohibited: "8+ in AI/ML"',
         r"\b(8|ten|eleven|11)\s*\+?\s*(years?)?\s*in\s*(AI/ML|AI)\b", True, 30),
        ("R11-3", "R11", 'prohibited: "led a team of product managers"',
         r"led a team of product managers", True, 31),
        ("R11-4", "R11", 'prohibited: "security career of N years"',
         r"security\s+(for|career of)\s+\d+\s*years?", True, 32),
        ("R2-1", "R2", "prohibited: full-time/part-time/hours-per-week",
         r"\b(full[- ]time|part[- ]time|hours per week|投入\d+小时)\b", True, 33),
        ("R2-2", "R2", 'prohibited: "portfolio since 2025-12"',
         r"portfolio since 20\d{2}-\d{2}", True, 34),
        ("R1-1", "R1", "English text must not use Ltd/Co.",
         r"\b(Ltd|Ltd\.|Co\.,Ltd|Inc\.)\b", True, 35),
        ("R7-1", "R7", "prohibited: reverse engineering in the title for IP-intensive employers",
         r"reverse engineer", False, 36),  # MOT/ACME are not IP-intensive; warning only (source-file comment recorded verbatim)
        ("R3-1", "R3", "Ngee Ann: prohibited from citing LLM/agent, and prohibited from writing deployed",
         r"(3D conversational avatar).{0,80}(LLM|agent)|deployed at (the )?polytechnic", True, 37),
        ("R6-1", "R6", "prohibited: jargon / crime-association words",
         r"\b(hack|exploit|crack|pwn|backdoor)\b", True, 38),
        ("R6-2", "R6", "prohibited: vague delivery verbs",
         r"\b(revolutionis|revolutioniz|world[- ]class|cutting[- ]edge|leverage synergies)\b", True, 39),
        ("R1-2", "R1", 'ISO21434 must express participation ("contributed to")',
         r"\bI\s+(?:led|owned|established)\b[^.]{0,60}ISO/?SAE\s*21434", True, 40),
        ("R5-1", "R5", "one metric must not be stated as two",
         r"(10|ten)\s*(percentage points|%).{0,60}(10|ten)\s*(percentage points|%)", True, 41),
    ]
    for rid, code, desc, pattern, hard, line in fail_rules:
        rules.append(Rule(
            id=rid, code=code, desc=desc, kind=KIND_REGEX, hard=hard,
            pattern=pattern, provenance=f"{_SRC}:L{line}",
        ))

    # ── ISO21434 conditional (source file L44-46, L90-92) ──────────────────────────
    rules.append(Rule(
        id="R1-iso", code="R1",
        desc="mentions ISO/SAE 21434 but does not write contributed to/working group",
        kind=KIND_CONDITIONAL, hard=True,
        trigger=r"ISO/?SAE\s*21434",
        satisfy=r"contributed to[^.]{0,140}ISO/?SAE\s*21434|ISO/?SAE\s*21434[^.]{0,80}working group",
        provenance=f"{_SRC}:L44-46,L90-92",
    ))

    # ── CUSTOM deterministic checks (source file L48-60, lambdas recorded verbatim) ────────────────────

    def patent_employer_assigned(t: str) -> list[tuple[int, str]]:
        return [
            (t[:m.start()].count("\n") + 1,
             t[max(0, m.start() - 30):m.end() + 60].replace("\n", " "))
            for m in re.finditer(r"\bpatents?\b", t, re.I)
            if not re.search(r"employer-assigned", t[m.start():m.start() + 70], re.I)
        ]

    def shipped_only_for_mass_produced(t: str) -> list[tuple[int, str]]:
        return [
            (t[:m.start()].count("\n") + 1, "shipped/deployed used for a non-mass-produced product")
            for m in re.finditer(r"\b(shipped|deployed)\b", t, re.I)
            if not re.search(
                r"mass-production|volume production|vehicles already|Institute for Infocomm Research|I2R",
                t[max(0, m.start() - 260):m.end() + 260], re.I)
        ]

    rules.append(Rule(
        id="R1-patent", code="R1", desc="every mention of patent must be marked employer-assigned nearby",
        kind=KIND_LAMBDA, hard=True, fn=patent_employer_assigned,
        provenance=f"{_SRC}:L50-54",
    ))
    rules.append(Rule(
        id="R5-shipped", code="R5", desc="shipped/deployed (delivery verbs) may only be used for already-mass-produced products",
        kind=KIND_LAMBDA, hard=True, fn=shipped_only_for_mass_produced,
        provenance=f"{_SRC}:L55-59",
    ))

    # ── MUST_HAVE required statements (source file L62-68) ───────────────────────────────
    must_have = [
        ("R1-must-patent", "R1", "patents employer-assigned", r"employer-assigned", 64),
        ("R11-must-years", "R11", "finalized tenure wording", r"eleven years in technology", 65),
        ("R2-must-gap", "R2", "gap-period caliber", r"(seven projects|393 commits|registered Dec 2025)", 66),
        ("R8-must-citizen", "R8", "SG format — header states only Singapore Citizen", r"Singapore Citizen", 67),
    ]
    for rid, code, desc, pattern, line in must_have:
        rules.append(Rule(
            id=rid, code=code, desc=desc, kind=KIND_MUST_HAVE, hard=True,
            must=pattern, provenance=f"{_SRC}:L{line}",
        ))

    return rules


# For regression comparison: _130's 10 historical CLs (source file L14-25, recorded verbatim)
HISTORICAL_CLS = [
    "CoverLetter_ACME_AI_Data_Office_EN.md",
    "CoverLetter_MOT_AI_Office_EN.md",
    "CoverLetter_ACME_AI_Data_Governance_EN.md",
    "CoverLetter_AGD_Business_Innovation_SAD_EN.md",
    "CoverLetter_AGD_Business_Innovation_AD_EN.md",
    "CoverLetter_Litware_Language_AI_DD_EN.md",
    "CoverLetter_STB_Data_AI_Product_Mgr_EN.md",
    "CoverLetter_Litware_Vehicle_Weapon_Network_EN.md",
    "CoverLetter_Litware_Vision_AI_RD_EN.md",
    "CoverLetter_Woodgrove_TRO_Transport_AI_Programme_EN.md",
]
