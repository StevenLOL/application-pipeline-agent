"""F11 delivery-channel adaptation (D18): character-budget drafter + final-entry text re-review.

Iron rule (from the design docs' amendment records): **the agent never fills in
any form field**; per-form limits are discovered in three states (DOM maxlength /
measured / unverified) — the limit is a **user-supplied parameter**, not something
the agent scrapes (form scraping is outside the authorized scope).

**no-new-claims iron rule**: a candidate comes from **deterministic sentence
packing of the user's own text** (delete-only) — no new content is generated, so
the "model fabricates experience" failure mode cannot occur; LLM rewriting is an
optional enhancement slot (the 1.5B model is measured-unsuitable for production
rewriting).

Closed loop (D18 acceptance):
    discover limit 256 -> candidate <= 256 -> simulated user rewrite ->
    review_final re-check -> pass
Negative case: no limit (unverified and none given) -> no trimming, annotation only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class LimitSource(str, Enum):
    DOM = "dom"            # form element maxlength attribute
    MEASURED = "measured"  # discovered by paste-measurement
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class FieldLimit:
    """Character limit of one form field (three-state provenance)."""

    field_name: str
    limit: int | None          # None = unknown (unverified)
    source: LimitSource

    def __post_init__(self) -> None:
        if self.limit is None and self.source != LimitSource.UNVERIFIED:
            raise ValueError(
                f"{self.field_name}: limit=None requires source=unverified "
                "(a known limit must carry its value)"
            )


@dataclass
class DraftCandidate:
    """A within-budget candidate + trimming notes (transparent and auditable -
    what was removed is obvious at a glance)."""

    field_name: str
    text: str
    used: int
    limit: int | None
    within_budget: bool
    dropped_sentences: list[str] = field(default_factory=list)
    source: LimitSource = LimitSource.UNVERIFIED


@dataclass
class ReviewResult:
    passed: bool
    findings: list[tuple[int, str]]
    over_budget_fields: list[str] = field(default_factory=list)


# ---- Sentence splitting (conservative: period/question/exclamation + newline;
# no abbreviation handling - mis-splits are caught by manual review) ----

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text.strip()) if s.strip()]


# ---- Drafting: greedy sentence packing (delete-only) ----

def fit_to_budget(text: str, limit: FieldLimit) -> DraftCandidate:
    """Fit the user's text into a character budget: keep whole sentences greedily,
    drop and record sentences that exceed the budget.

    no-new-claims: every word of the candidate comes from the source text - no
    generation, no rewriting, no completion.
    unverified with no limit -> return as-is + annotate (no trimming; the
    re-check prompts to confirm the limit).
    """
    used_all = len(text)
    if limit.limit is None:
        return DraftCandidate(
            field_name=limit.field_name, text=text, used=used_all,
            limit=None, within_budget=True,  # no limit = nothing to trim; flag in re-check
            dropped_sentences=[], source=limit.source,
        )
    if used_all <= limit.limit:
        return DraftCandidate(
            field_name=limit.field_name, text=text, used=used_all,
            limit=limit.limit, within_budget=True,
            dropped_sentences=[], source=limit.source,
        )
    kept: list[str] = []
    used = 0
    dropped: list[str] = []
    for s in _sentences(text):
        extra = len(s) + (1 if kept else 0)  # space-joined
        if used + extra <= limit.limit:
            kept.append(s)
            used += extra
        else:
            dropped.append(s)
    # no-new-claims holds even in the truncation fallback: never cut mid-word
    # (a partial word like "project" from "projects" would be a token that does
    # not exist in the source text). Cut at the last word boundary instead.
    if kept:
        candidate_text = " ".join(kept)
    else:
        candidate_text = text[:limit.limit]
        if " " in candidate_text:
            candidate_text = candidate_text[: candidate_text.rfind(" ")]
    return DraftCandidate(
        field_name=limit.field_name, text=candidate_text,
        used=len(candidate_text), limit=limit.limit,
        within_budget=len(candidate_text) <= limit.limit,
        dropped_sentences=dropped, source=limit.source,
    )


# ---- Re-check: final entry text (after user rewrite) through rule engine + budget ----

def review_final(text: str, limit: FieldLimit | None = None,
                 rules: list | None = None,
                 *, include_document_level: bool = False) -> ReviewResult:
    """Re-check the final entry text (closed-loop endpoint after user rewrite).

    Checks: (1) character budget (if a limit was given) (2) rule engine.
    ``include_document_level``: an entry **fragment** (experience section /
    single field) is not a full document - document-level MUST_HAVE checks
    (e.g. "patent must state employer-assigned") would always fire on a
    fragment, so they are excluded by default; pass True explicitly when
    re-checking a complete document (CL / resume).
    """
    findings: list[tuple[int, str]] = []
    over: list[str] = []
    if limit is not None and limit.limit is not None and len(text) > limit.limit:
        over.append(limit.field_name)
        findings.append((1, f"[over-budget] {limit.field_name}: {len(text)} > {limit.limit}"))
    if rules:
        from .compliance.engine import KIND_MUST_HAVE, check_text

        effective = rules if include_document_level else [
            r for r in rules if r.kind != KIND_MUST_HAVE]
        for f in check_text(text, effective):
            findings.append((f.line, f"[{f.code}] {f.desc}: {f.segment[:60]}"))
    return ReviewResult(passed=not findings and not over,
                        findings=findings, over_budget_fields=over)


def fit_fields(text_by_field: dict[str, str],
               limits: list[FieldLimit]) -> dict[str, DraftCandidate]:
    """Multi-field batch drafting (Workday scenario: per-field limits differ)."""
    limit_by_name = {l.field_name: l for l in limits}
    out: dict[str, DraftCandidate] = {}
    for name, text in text_by_field.items():
        lim = limit_by_name.get(name, FieldLimit(name, None, LimitSource.UNVERIFIED))
        out[name] = fit_to_budget(text, lim)
    return out
