"""Extraction-layer unit tests (schema validation + agreement statistics —
purely deterministic, real network forbidden).

LM behavior is covered by the acceptance tests
(tests/acceptance/test_extract_jd.py, skipif the local service is down).
"""

from __future__ import annotations

import pytest

from apa.extract import (
    ConsistencyResult,
    validate_assessments,
    validate_deadline,
    validate_requirements,
)


class TestValidateDeadline:
    @pytest.mark.parametrize("v", ["2026-10-22", "NOT_PUBLISHED"])
    def test_accepts(self, v):
        ok, val = validate_deadline(v)
        assert ok and val == v

    @pytest.mark.parametrize("raw,expected", [
        ("22 Oct 2026", "2026-10-22"),
        ("22 October 2026", "2026-10-22"),
        ("Oct 22, 2026", "2026-10-22"),
        ("October 22 2026", "2026-10-22"),
    ])
    def test_normalizes_english_dates(self, raw, expected):
        """The model extracts the date from the source text; the code layer
        normalizes it (architecture: conversion is not the model's job)."""
        ok, val = validate_deadline(raw)
        assert ok and val == expected

    @pytest.mark.parametrize("v", ["", "next week", "2026/10/22", None])
    def test_rejects(self, v):
        ok, err = validate_deadline(v)
        assert not ok and isinstance(err, str)


class TestHydrateAssessments:
    """Quote location at the code layer: the model gives the name, the code
    takes evidence from the source text; unsupported names are dropped."""

    JD = (
        "Building and Construction Authority. Senior Manager (AI Engineer).\n"
        "Pass the online technical assessment administered by the Government "
        "Technology Agency's Digital Academy and submit the official results "
        "to Contoso as part of the recruitment process.\n"
        "PMP, Agile, Scrum certifications are a plus."
    )

    def test_locate_quote_verbatim(self):
        from apa.extract import hydrate_assessments

        items, dropped = hydrate_assessments(
            self.JD, ["CivicTech Digital Academy online technical assessment"])
        assert not dropped
        assert items[0]["quote"] in self.JD
        assert "Digital Academy" in items[0]["quote"]

    def test_unsupported_name_dropped(self):
        """Names without support in the source text are dropped — mechanizing
        the accident-4 defense."""
        from apa.extract import hydrate_assessments

        items, dropped = hydrate_assessments(
            self.JD, ["WonderCorp Psychometric Battery"])
        assert not items and dropped == ["WonderCorp Psychometric Battery"]

    def test_keyword_fallback_match(self):
        """When the model's wording differs slightly, fall back to
        content-word co-occurrence matching."""
        from apa.extract import hydrate_assessments

        items, dropped = hydrate_assessments(self.JD, ["Digital Academy assessment"])
        assert items and "Digital Academy" in items[0]["quote"]

    def test_empty_names_ok(self):
        from apa.extract import hydrate_assessments

        items, dropped = hydrate_assessments(self.JD, [])
        assert items == [] and dropped == []


class TestValidateItems:
    def test_requirements_ok(self):
        ok, data = validate_requirements(
            '[{"category": "must", "quote": "Bachelor\'s degree in computer science"}]'
        )
        assert ok and data[0]["category"] == "must"

    def test_missing_quote_rejected(self):
        ok, err = validate_requirements('[{"category": "must", "quote": "  "}]')
        assert not ok and "empty quote" in err

    def test_not_json_rejected(self):
        ok, err = validate_requirements("Bachelor's degree required")
        assert not ok and "JSON array" in err

    def test_assessments_empty_array_ok(self):
        """A JD with no assessment stage → [] is a legal value (the legal exit
        of the accident-4 defense)."""
        ok, data = validate_assessments("[]")
        assert ok and data == []


class TestConsistency:
    def _mk(self, runs, valid):
        c = ConsistencyResult(field_name="deadline")
        c.runs = runs
        c.valid_runs = valid
        return c

    def test_full_agreement(self):
        c = self._mk(["2026-10-22"] * 5, 5)
        assert c.agreement == 1.0 and c.schema_ok_rate == 1.0
        assert not c.needs_human

    def test_low_agreement_needs_human(self):
        c = self._mk(["2026-10-22", "2026-10-23", "NOT_PUBLISHED", "2026-10-22", "2026-10-22"], 5)
        assert 0.5 < c.agreement < 0.8
        assert c.needs_human  # agreement <0.8 → escalate to human

    def test_schema_failures_needs_human(self):
        c = self._mk(["2026-10-22", None, None, "2026-10-22", "2026-10-22"], 3)
        assert c.schema_ok_rate == 0.6
        assert c.needs_human  # schema-valid rate <0.8 → escalate to human

    def test_empty_runs_needs_human(self):
        c = self._mk([], 0)
        assert c.needs_human
