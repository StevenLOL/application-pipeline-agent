"""F3 extractor (stage 9): DSPy Predict wrapping + schema validation + 5-run agreement + low-confidence escalation to human review.

Architecture discipline:
- The LM is configured uniformly via `apa.llm.load_settings()` (local llama.cpp by default; cloud private
  opt-in + the Presidio material gate unchanged);
- Extraction results are **suggestions**, not facts: if the schema fails or the 5-run agreement rate exceeds
  the threshold, mark `needs_human` (the "low extraction confidence" HITL point in the design docs), to be
  adjudicated by a human;
- The purely local deterministic parts (schema validation/agreement statistics) are separated from the LM
  calls — the former are unit-testable, the latter connect to the local service at acceptance time
  (skipif llama.cpp is absent).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .signatures import ExtractAssessment, ExtractDeadline, ExtractRequirements

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NOT_PUBLISHED = "NOT_PUBLISHED"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "22 Oct 2026" / "22 October 2026" / "Oct 22, 2026" / "October 22 2026"
_DMY_RE = re.compile(r"^(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})$")
_MDY_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")


def normalize_date(value: str) -> Optional[str]:
    """Normalize common English date forms to YYYY-MM-DD (a deterministic code-layer conversion).

    The model is only responsible for extracting the source text (measured in stage 9: making a 1.5B do
    "extraction + format conversion" at the same time triggers conservative escape); the conversion happens
    here — the same idea as the A4 entity normalization.
    """
    v = (value or "").strip()
    if _DATE_RE.match(v):
        return v
    m = _DMY_RE.match(v)
    if m and m.group(2).lower()[:3] in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()[:3]]:02d}-{int(m.group(1)):02d}"
    m = _MDY_RE.match(v)
    if m and m.group(1).lower()[:3] in _MONTHS:
        return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d}"
    return None


# ---- schema validation (deterministic, unit-testable) ----

def _try_json(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def validate_deadline(value: str) -> tuple[bool, str]:
    """YYYY-MM-DD / NOT_PUBLISHED / common English date source text (the latter normalized to YYYY-MM-DD)."""
    v = (value or "").strip()
    if v == NOT_PUBLISHED:
        return True, v
    norm = normalize_date(v)
    if norm:
        return True, norm
    return False, f"deadline must be YYYY-MM-DD, NOT_PUBLISHED, or a parseable date, got {value!r}"


def validate_items(value: str, required_keys: set[str]) -> tuple[bool, Any]:
    """JSON array + every item has all required keys + a non-empty quote."""
    data = _try_json(value or "")
    if not isinstance(data, list):
        return False, f"expected JSON array, got {value!r:.80}"
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not required_keys <= set(item):
            return False, f"item[{i}] missing keys {required_keys}: {item!r:.80}"
        if not str(item.get("quote", "")).strip():
            return False, f"item[{i}] has empty quote"
    return True, data


def validate_requirements(value: str) -> tuple[bool, Any]:
    return validate_items(value, {"category", "quote"})


def validate_assessments(value: str) -> tuple[bool, Any]:
    """Candidate name list: a JSON array of strings, each non-empty."""
    data = _try_json(value or "")
    if not isinstance(data, list):
        return False, f"expected JSON array of names, got {value!r:.80}"
    for i, item in enumerate(data):
        if not isinstance(item, str) or not item.strip():
            return False, f"item[{i}] must be a non-empty string: {item!r:.60}"
    return True, data


def _locate_quote(jd_text: str, name: str) -> Optional[str]:
    """Locate the verbatim quote of a name in the JD source text (sentence scoring: take the highest-scoring sentence by content-word hit rate).

    Deterministic evidence-taking: the quote is sliced from the source text by code, never transcribed by
    the model — model transcription introduces non-verbatim risk, while the entire effect of A4
    claim-verification rests on the quote being verbatim.

    Scoring rule: a sentence is adopted only if (content words of name hit in the sentence) / (total content
    words of name) ≥ 0.5 (measured in stage 9: the source text "…Digital Academy and submit…" and the model name
    "…Digital Academy online technical assessment" have different word orders — both verbatim containment
    and fixed first-3-word co-occurrence would miss; the scoring method passes with 5/6 hits).
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", jd_text) if s.strip()]
    words = {w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 3}
    if not words:
        return None
    best: Optional[str] = None
    best_hits = 0
    for s in sentences:
        low = s.lower()
        hits = sum(1 for w in words if w in low)
        if hits > best_hits:
            best, best_hits = s, hits
    if best is not None and best_hits >= max(1, len(words) * 0.5):
        return best
    return None


def hydrate_assessments(jd_text: str, names: list[str]) -> tuple[list[dict], list[str]]:
    """Candidate name → {name, quote} items; **names whose quote cannot be located are dropped** (unsupported claims are not trusted).

    Returns (items, dropped). dropped records the dropped names — this is the mechanization of the
    accident-4 defense: an assessment name hallucinated by the model finds no sentence in the source
    text and is eliminated directly.
    """
    items, dropped = [], []
    for name in names:
        quote = _locate_quote(jd_text, name)
        if quote is None:
            dropped.append(name)
            continue
        items.append({"name": name, "quote": quote})
    return items, dropped


VALIDATORS: dict[str, Callable[[str], tuple[bool, Any]]] = {
    "requirements": validate_requirements,
    "deadline": validate_deadline,
    "assessments": validate_assessments,
}


# ---- agreement rate (deterministic statistics) ----

@dataclass
class ConsistencyResult:
    """The 5-run (n-run) agreement rate for one extraction target."""

    field_name: str
    runs: list[Any] = field(default_factory=list)
    valid_runs: int = 0

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def agreement(self) -> float:
        """Mode share (field-level agreement rate)."""
        if not self.runs:
            return 0.0
        from collections import Counter

        c = Counter(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in self.runs)
        return c.most_common(1)[0][1] / len(self.runs)

    @property
    def schema_ok_rate(self) -> float:
        return self.valid_runs / self.n if self.n else 0.0

    @property
    def needs_human(self) -> bool:
        """Low confidence: schema failure rate >20% or agreement <0.8 → escalate to human review."""
        if self.n == 0:
            return True
        return self.schema_ok_rate < 0.8 or self.agreement < 0.8


@dataclass
class ExtractionResult:
    jd_ref: str
    values: dict[str, Any] = field(default_factory=dict)
    consistency: dict[str, ConsistencyResult] = field(default_factory=dict)
    schema_errors: dict[str, str] = field(default_factory=dict)
    dropped_assessments: list[str] = field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return bool(self.schema_errors) or any(
            c.needs_human for c in self.consistency.values()
        )


# ---- extractor ----

class JDExtractor:
    """DSPy Predict wrapping: one Predict per target + validation."""

    def __init__(self, lm: Any = None):
        import dspy

        if lm is not None:
            dspy.configure(lm=lm)
        self._predictors = {
            "requirements": dspy.Predict(ExtractRequirements),
            "deadline": dspy.Predict(ExtractDeadline),
            "assessments": dspy.Predict(ExtractAssessment),
        }

    def set_predictor(self, target: str, predictor: Any) -> None:
        """Inject a test double (FakePredictor) — mock the interface, not the mechanism (testing discipline)."""
        if target not in self._predictors:
            raise ValueError(f"unknown target: {target!r}")
        self._predictors[target] = predictor

    def extract_one(self, target: str, jd_text: str) -> tuple[Any, Optional[str], dict]:
        """Single extraction + schema validation (+ quote location for assessments).

        Returns (value, error message or None, meta). assessments' meta contains
        ``dropped`` — names given by the model with no quote support found in the source text (the
        accident-4 defense eliminations).
        """
        raw = getattr(self._predictors[target](jd_text=jd_text), target)
        raw = raw if isinstance(raw, str) else str(raw)
        ok, value_or_err = VALIDATORS[target](raw)
        if not ok:
            return None, value_or_err, {}
        if target == "assessments":
            items, dropped = hydrate_assessments(jd_text, value_or_err)
            if value_or_err and not items:
                return None, (
                    f"assessment names lack quote support in JD (dropped): {value_or_err}"
                ), {"dropped": dropped}
            return items, None, {"dropped": dropped}
        return value_or_err, None, {}

    def extract_with_consistency(
        self, jd_text: str, jd_ref: str = "", n: int = 5
    ) -> ExtractionResult:
        """Run n full-target extractions over one JD, aggregating agreement and the low-confidence decision."""
        result = ExtractionResult(jd_ref=jd_ref)
        for target in ("requirements", "deadline", "assessments"):
            cons = ConsistencyResult(field_name=target)
            for _ in range(n):
                value, err, meta = self.extract_one(target, jd_text)
                if err:
                    result.schema_errors.setdefault(target, err)
                    if meta.get("dropped"):
                        result.dropped_assessments.extend(meta["dropped"])
                else:
                    cons.valid_runs += 1
                    result.values[target] = value  # the last successful value
                cons.runs.append(value)
            result.consistency[target] = cons
        return result


def configure_from_env(**overrides: Any) -> Any:
    """Configure the DSPy LM via apa.llm (local llama.cpp by default / cloud private opt-in)."""
    import dspy

    from ..llm.client import load_settings

    settings = load_settings(**overrides)
    return dspy.LM(
        model=f"openai/{settings.model}",
        api_base=settings.base_url,
        api_key=settings.api_key,
        max_tokens=1024,
        temperature=0.0,
    )
