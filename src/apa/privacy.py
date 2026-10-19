"""F6/G5 privacy gate (R3): blocking PII checks before egress LLM calls + canary leak tests.

Landing of the JD's "Responsible AI: privacy leakage checks":
- **PIIGate**: Presidio Analyzer (en_core_web_lg) + a custom **Singapore NRIC recognizer**
  ([STFG]\d{7}[A-Z] - not built into standard Presidio; this repo fills the gap for the SG job-hunting scenario);
- **blocking semantics**: before egress-backend (workbuddy/cloud) calls,
  `assert_safe` raises `PrivacyLeak` on detection - never silently released,
  never silently masked (masking is provided by `mask`, for use after human
  confirmation);
- **canary leak tests**: inject fake PII -> walk the egress call path -> assert
  the fake client received zero canary content (in tests/; the mechanism is
  "inject and the interception must be detected", not "trust that interception
  exists");
- integration point: `apa.llm.chat(material_cleared=False)` forces this gate to run
  (fail-closed placeholder before R3 -> real checks from R3 on).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .llm.client import PresidioGateRequired

# SG NRIC/FIN: leading letter S/T/F/G + 7 digits + check letter (no check-digit
# algorithm - detection relies on shape, with human confirmation as the
# false-positive backstop; the miss risk is quantified by canary tests)
_NRIC_RE = re.compile(r"\b[STFGstfg]\d{7}[A-Za-z]\b")
# Phone (Singapore 8-digit, +65 optional)
_SG_PHONE_RE = re.compile(r"(?:\+65[\s-]?)?[689]\d{3}[\s-]?\d{4}\b")


@dataclass
class PIIFinding:
    kind: str
    start: int
    end: int
    sample: str  # masked sample (never the full value - audit files themselves must not contain PII)


class PIIGate:
    """PII checks before egress (regex layer always on + optional Presidio NER enhancement)."""

    def __init__(self, use_presidio: bool = True, presidio_model: str = "en_core_web_lg"):
        self._analyzer = None
        self._presidio_requested = use_presidio
        self._model_name = presidio_model

    def _ensure_analyzer(self) -> Optional[Any]:
        """Lazily load Presidio (on first scan); if unavailable the regex layer still works."""
        if self._analyzer is not None or not self._presidio_requested:
            return self._analyzer
        try:
            from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer

            nlp_engine = self._build_nlp_engine()
            self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine, default_score_threshold=0.4)
            # Custom SG NRIC recognizer (denominator=0.75: strong shape signal)
            # presidio 2.2.364: patterns require Pattern objects (bare dicts report missing compiled_regex)
            nric = PatternRecognizer(
                supported_entity="SG_NRIC", patterns=[
                    Pattern(name="sg nric", regex=_NRIC_RE.pattern, score=0.75)])
            self._analyzer.registry.add_recognizer(nric)
        except Exception:
            self._analyzer = False  # Presidio unavailable -> regex-only (honest degradation, noted in the report)
        return self._analyzer

    def _build_nlp_engine(self) -> Optional[Any]:
        """spaCy NLP engine: prefer the local en_core_web_lg, fall back to small/empty config."""
        try:
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            import spacy

            model = self._model_name
            if not spacy.util.is_package(model):
                model = "en_core_web_sm" if spacy.util.is_package("en_core_web_sm") else ""
            conf = ({"nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": model}]}
                    if model else {})
            provider = NlpEngineProvider(nlp_configuration=conf)
            return provider.create_engine()
        except Exception:
            return None

    def scan(self, text: str) -> list[PIIFinding]:
        """Return the list of PII findings (regex always on; NER layered on when Presidio is available)."""
        findings: list[PIIFinding] = []
        seen: set[tuple[int, int]] = set()

        def _add(kind: str, m: re.Match) -> None:
            if (m.start(), m.end()) not in seen:
                seen.add((m.start(), m.end()))
                masked = m.group(0)[:3] + "***"
                findings.append(PIIFinding(kind, m.start(), m.end(), masked))

        for m in _NRIC_RE.finditer(text):
            _add("SG_NRIC", m)
        for m in _SG_PHONE_RE.finditer(text):
            _add("SG_PHONE", m)
        for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.]+", text):
            _add("EMAIL", m)

        analyzer = self._ensure_analyzer()
        if analyzer:
            for r in analyzer.analyze(text=text, language="en"):
                if (r.start, r.end) not in seen:
                    seen.add((r.start, r.end))
                    findings.append(PIIFinding(
                        r.entity_type, r.start, r.end, text[r.start:r.start + 3] + "***"))
        return findings

    def assert_safe(self, text: str, *, label: str = "material") -> None:
        """Blocking semantics: raise immediately on PII detection (callers should mask or get human confirmation first)."""
        findings = self.scan(text)
        if findings:
            kinds = sorted({f.kind for f in findings})
            raise PresidioGateRequired(
                f"{label} contains {len(findings)} PII finding(s): {kinds} - "
                "mask first (PIIGate.mask) or use a cleared version"
            )

    def mask(self, text: str) -> str:
        """Replace PII with <TYPE> placeholders (for the sanitized version after human confirmation)."""
        out = text
        for f in sorted(self.scan(text), key=lambda f: f.start, reverse=True):
            out = out[:f.start] + f"<{f.kind}>" + out[f.end:]
        return out
