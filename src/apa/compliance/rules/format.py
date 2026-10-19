"""F1 rule set, part 3: SG format requirements (the format family).

Source: singapore master conventions (planning doc section 5, resume red line 8 "SG format: reverse
chronological order; no tables/multi-column; British spelling; header states only Singapore Citizen;
no NRIC/age/photo") + the actual CL structure of delivery packages (25 packages, consistent shape).
**All warning level (hard=False)**: a missing format item is surfacing, confirmed by a human — the md
line count is a rough proxy for PDF page count, and even more of an estimate.

Boundary with the inherited set: the inherited 12 red lines are **wording-level** (what to write); this
family is **format-level** (how to lay it out) — the record-verbatim discipline is unaffected (new rules
in a new file; the inherited files are untouched).

Applicability: CL structure rules apply only to CLs containing salutation/sign-off features (JD/resumes are
automatically skipped); resume rules apply only to documents containing a Singapore Citizen header or an
Experience feature.
"""

from __future__ import annotations

import re

from ..engine import KIND_LAMBDA, Rule

_SRC = "singapore master conventions (planning doc section 5, resume red lines) + measured delivery-package structure"

# ── document type features ──
_CL_FEATURE_RE = re.compile(r"Dear Hiring Manager|Yours faithfully|Yours sincerely", re.I)
_RESUME_FEATURE_RE = re.compile(r"Singapore Citizen|WORK EXPERIENCE|Experience\s*$", re.I | re.M)

# ── CL structure four-piece set ──
_DATE_LINE_RE = re.compile(r"^\s*\d{1,2}\s+[A-Z][a-z]+\s+20\d{2}\s*$", re.M)
_RE_LINE_RE = re.compile(r"^\s*\*{0,2}Re[.:]", re.M)
_SALUTATION_RE = re.compile(r"Dear\b", re.M)
_SIGNOFF_RE = re.compile(r"Yours (faithfully|sincerely)", re.I)

# ── resume header and prohibited items ──
# Work-authorization status: the first screening item for SG employers (consistent across multiple
# sources: matchcv/CVEdge/TAFEP). **Depends on the individual's own status** (corrected 2026-10-19):
# any form of Citizen/PR/EP/needs-sponsorship is acceptable — the rule requires that "a status statement
# exists", not a specific value (hardcoding one particular identity as a general rule would leak
# "user-as-self" into generic code; corrected).
_WORK_AUTH_RE = re.compile(
    r"Singapore Citizen|Singapore\s*PR|Permanent Resident"
    r"|Employment Pass|S\s*Pass|EP\s*holder|work pass holder"
    r"|requires?\s+(?:sponsorship|work pass)"
    r"|无需工作签证|公民|永久居民",
    re.I,
)
_NRIC_FULL_RE = re.compile(r"\b[STFG]\d{7}[A-Z]\b")
_PHOTO_WORD_RE = re.compile(r"\bphoto\b|\bphotograph\b", re.I)
_AGE_WORD_RE = re.compile(r"\bage[:\s]*\d{2}\b|\b\d{2}\s*years old\b", re.I)
_MD_TABLE_RE = re.compile(r"^\s*\|.+\|\s*$", re.M)

# ── reverse-chronological timeline (work year pairs) ──
# Measured formats: delivery-package resumes use "2019-10 to present" (YYYY-MM) and also tolerate "2019 - 2021".
# Structure: YM (to | dash) (YM | present) — the "-" in YYYY-MM is an intra-date separator; the range
# separator is "to" or the hyphen that follows it (measured in stage 10: the first version treated "-10"
# as a range separator, causing misses).
_YEAR = r"(19[89]\d|20[0-2]\d)"
_YM = _YEAR + r"(?:-\d{2})?"
_YEAR_RANGE_RE = re.compile(rf"\b{_YM}\s*(?:to|[-–—])\s*(?:{_YM}|present)\b", re.I)
_MONTH_YEAR_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20[0-2]\d\b", re.I)

# ── British spelling consistency ──
_UK_SPELL_RE = re.compile(r"\b\w*organis\w*|\b\w*analys\w*|\b\w*centre\b|\b\w*colour\b", re.I)
_US_SPELL_RE = re.compile(r"\b\w*organiz\w*|\b\w*analyz\w*|\b\w*center\b|\b\w*color\b", re.I)

# Length proxy (md line count → a rough estimate of PDF page count — non-linear; only catches "obviously over").
# Research calibration (2026-10-19, 5 sources): Resume 1-2 pages (3 pages = unedited, CVEdge);
# CL one page max (consistent across sources). Thresholds are loosened to "obviously over" to avoid
# md/PDF mapping false positives.
_CL_LINE_BUDGET = 90
_RESUME_LINE_BUDGET = 200


def _is_cl(text: str) -> bool:
    return bool(_CL_FEATURE_RE.search(text))


def _is_resume(text: str) -> bool:
    return bool(_RESUME_FEATURE_RE.search(text)) and not _is_cl(text)


def cl_structure_check(text: str) -> list[tuple[int, str]]:
    """CL structure four-piece set missing → (line number, note). Applies to CLs only."""
    if not _is_cl(text):
        return []
    out: list[tuple[int, str]] = []
    checks = [
        ("date line (DD Month YYYY)", _DATE_LINE_RE),
        ("Re: subject line", _RE_LINE_RE),
        ("salutation (Dear ...)", _SALUTATION_RE),
        ("sign-off (Yours faithfully/sincerely)", _SIGNOFF_RE),
    ]
    for name, rx in checks:
        if not rx.search(text):
            out.append((1, f"[CL structure missing] {name} not found — standard SG CL structure ({_SRC})"))
    n_lines = len(text.splitlines())
    if n_lines > _CL_LINE_BUDGET:
        out.append((1, f"[CL length] {n_lines} lines exceeds the 1-page proxy budget ({_CL_LINE_BUDGET})"
                       " — line count is a rough proxy for page count; confirm manually"))
    return out


def resume_header_check(text: str) -> list[tuple[int, str]]:
    """Resume header norms: work-authorization status required (wording depends on the individual's own
    status) + no full NRIC/age/photo wording (TAFEP applies to everyone)."""
    if not _is_resume(text):
        return []
    out: list[tuple[int, str]] = []
    if not _WORK_AUTH_RE.search(text):
        out.append((1, "[resume header missing] work-authorization status not stated (Singapore Citizen / PR / "
                       "EP holder / requires sponsorship — write the one that matches your status; the first "
                       "screening item for SG employers)"))
    for m in _NRIC_FULL_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append((line, "[resume prohibited item] full NRIC/FIN number present — SG resumes must not include NRIC (also a privacy red line)"))
    for m in _PHOTO_WORD_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append((line, "[resume suggestion] the word photo — TAFEP fair-employment standards discourage photos "
                          "(an old convention, inconsistent sources: some templates still suggest it; the "
                          "government framework takes precedence)"))
    for m in _AGE_WORD_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append((line, "[resume prohibited item] age statement — SG resumes must not include age"))
    return out


def reverse_chronology_check(text: str) -> list[tuple[int, str]]:
    """Reverse-chronological timeline: year ranges should be non-increasing in order of appearance
    (ties within the same section are allowed).

    Conservative implementation: only the **start years** of two adjacent year ranges are compared;
    a later start year greater than the previous one is flagged (education-before-work interleaving
    will produce false positives — warning level, confirmed by a human).
    """
    if not _is_resume(text):
        return []
    out: list[tuple[int, str]] = []
    spans: list[tuple[int, int, str]] = []  # (start_year, line, matched)
    for m in _YEAR_RANGE_RE.finditer(text):
        end_year = int(m.group(2)) if m.group(2) else 2100  # present
        spans.append((int(m.group(1)), text[:m.start()].count("\n") + 1, m.group()))
    for (s1, l1, t1), (s2, l2, t2) in zip(spans, spans[1:]):
        if s2 > s1:
            out.append((l2, f"[reverse-chronology question] '{t2}' (start {s2}) appears after '{t1}' (start {s1})"
                            " — not reverse-chronological; education/certificate year interleaving produces "
                            "false positives, confirm manually"))
    # length
    n_lines = len(text.splitlines())
    if n_lines > _RESUME_LINE_BUDGET:
        out.append((1, f"[resume length] {n_lines} lines exceeds the 2-page proxy budget ({_RESUME_LINE_BUDGET})"
                       " — line count is a rough proxy for page count; confirm manually"))
    return out


def no_table_check(text: str) -> list[tuple[int, str]]:
    """No tables/multi-column (markdown table rows) — applies to both CLs and resumes."""
    if not (_is_cl(text) or _is_resume(text)):
        return []
    out: list[tuple[int, str]] = []
    for m in _MD_TABLE_RE.finditer(text):
        line = text[:m.start()].count("\n") + 1
        out.append((line, "[tables prohibited] markdown table row — SG resumes/CLs prohibit tables/multi-column (ATS parsing risk)"))
    return out


def british_spelling_check(text: str) -> list[tuple[int, str]]:
    """British spelling consistency (SG convention is British): a US spelling prompts unification;
    UK and US mixed together = an even clearer inconsistency."""
    if not (_is_cl(text) or _is_resume(text)):
        return []
    uk = len(_UK_SPELL_RE.findall(text))
    us = len(_US_SPELL_RE.findall(text))
    out: list[tuple[int, str]] = []
    if uk and us:
        line = None
        for m in _US_SPELL_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            break  # report the position of the first US spelling
        out.append((line or 1, f"[spelling mix] British ({uk} occurrence(s)) and American ({us} occurrence(s)) spellings mixed"
                               " — SG convention is British; unify the spelling"))
    elif us and not uk:
        line = None
        for m in _US_SPELL_RE.finditer(text):
            line = text[:m.start()].count("\n") + 1
            break
        out.append((line or 1, f"[spelling convention] American spellings ({us} occurrence(s), e.g. organiz/center)"
                               " — SG convention is British; consider unifying to British (organis/centre)"))
    return out


def format_rules() -> list[Rule]:
    return [
        Rule(
            id="fmt-cl-structure", code="FMT", desc="CL structure four-piece set (date/Re:/salutation/sign-off) + length",
            kind=KIND_LAMBDA, hard=False, fn=cl_structure_check,
            provenance=_SRC,
        ),
        Rule(
            id="fmt-resume-header", code="FMT", desc="resume header must state Singapore Citizen + no NRIC/age/photo",
            kind=KIND_LAMBDA, hard=False, fn=resume_header_check,
            provenance=_SRC,
        ),
        Rule(
            id="fmt-reverse-chrono", code="FMT", desc="reverse-chronological timeline + resume length",
            kind=KIND_LAMBDA, hard=False, fn=reverse_chronology_check,
            provenance=_SRC,
        ),
        Rule(
            id="fmt-no-table", code="FMT", desc="no tables/multi-column (CL/resume)",
            kind=KIND_LAMBDA, hard=False, fn=no_table_check,
            provenance=_SRC,
        ),
        Rule(
            id="fmt-british-spelling", code="FMT", desc="British spelling consistency (mixing is flagged)",
            kind=KIND_LAMBDA, hard=False, fn=british_spelling_check,
            provenance=_SRC,
        ),
    ]
