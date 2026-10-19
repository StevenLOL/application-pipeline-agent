"""FakeLM / FakePredictor unit tests (layering tests): full-chain offline
verification of the extract mechanism.

Principle: mock the interface, not the mechanism — what is under test is the
extractor's deterministic parts (validation / normalization / hydration /
agreement / needs_human); model quality belongs to the evaluation layer
(tests/acceptance/test_extract_jd.py connects to the real service).

Layering implementation: FakePredictor directly simulates the predictor call
interface (bypassing the DSPy Adapter's secondary parsing — measured: feeding
raw text gets interfered with by ChatAdapter/JSONAdapter).
"""

from __future__ import annotations

import pytest

from apa.extract import JDExtractor
from apa.llm import FakeLM, FakePredictor

Contoso_LIKE = (
    "Building and Construction Authority. Senior Manager (AI Engineer). "
    "Fixed Terms. Closing on 22 Oct 2026. Pass the online technical "
    "assessment administered by the Civic Technology Agency's Digital "
    "Academy and submit the official results to Contoso."
)


def _offline_extractor(outputs: dict[str, list[str]]) -> JDExtractor:
    """Offline extractor: all three targets injected with FakePredictor (no
    LM service connection at all).

    Targets not explicitly specified get default legal output
    (extract_with_consistency loops over all three targets; the 1.5B boundary
    is known — the requirements default is the minimal schema-valid output).
    """
    ex = JDExtractor(lm=FakeLM(responses=[]))
    defaults = {
        "requirements": ['[{"category": "must", "quote": "Bachelor degree required"}]'] * 5,
        "deadline": ['{"deadline": "2026-10-22"}'] * 5,
        "assessments": ["[]"] * 5,
    }
    for target in ("requirements", "deadline", "assessments"):
        ex.set_predictor(target, FakePredictor(target, outputs.get(target, defaults[target])))
    return ex


def test_offline_deadline_extraction_five_runs() -> None:
    """deadline 5-run full chain (validation + normalization + agreement),
    zero network.

    Note: the Fake output of a single-value target (deadline) is a **bare
    value** (the model extracts the source-text form "22 Oct 2026"); JSON
    wrapping is the shape of list targets — the division of labor measured
    during development.
    """
    ex = _offline_extractor({"deadline": ["22 Oct 2026"] * 5})
    result = ex.extract_with_consistency(Contoso_LIKE, jd_ref="FAKE-1", n=5)

    cons = result.consistency["deadline"]
    assert cons.agreement == 1.0 and cons.valid_runs == 5
    assert result.values["deadline"] == "2026-10-22"  # the code-layer normalization takes effect
    assert not result.needs_human  # the deadline target is in full agreement


def test_offline_faked_outputs_exhausted_fails_loud() -> None:
    """Presets exhausted → explicit failure (no silent repetition) — mocks
    must fail-loud too."""
    ex = _offline_extractor({"deadline": ['{"deadline": "2026-10-22"}'] * 2})
    with pytest.raises(AssertionError, match="preset outputs exhausted"):
        ex.extract_with_consistency(Contoso_LIKE, jd_ref="FAKE-1", n=5)


def test_offline_needs_human_on_schema_garbage() -> None:
    """Fake returns schema garbage → needs_human triggers (mechanism tests do
    not depend on the model)."""
    ex = _offline_extractor({"deadline": ["deadline is next week"] * 5})
    result = ex.extract_with_consistency(Contoso_LIKE, jd_ref="FAKE-1", n=5)
    assert "deadline" in result.schema_errors
    assert result.needs_human


def test_offline_assessment_hydration_end_to_end() -> None:
    """assessments: Fake gives a name list → the code layer hydrates quotes →
    unsupported items are dropped.

    extract_one pops one entry per call: the two calls verify "supported →
    accepted" and "unsupported → dropped" respectively.
    """
    ex = _offline_extractor({})
    ex.set_predictor("assessments", FakePredictor("assessments", [
        '["CivicTech Digital Academy online technical assessment"]',
        '["WonderCorp Psychometric Battery"]',  # no source-text support → dropped
    ]))
    value, err, meta = ex.extract_one("assessments", Contoso_LIKE)
    assert err is None
    assert len(value) == 1 and "Digital Academy" in value[0]["quote"]
    assert meta["dropped"] == []

    value2, err2, meta2 = ex.extract_one("assessments", Contoso_LIKE)
    assert err2 is not None and "quote support" in err2
    assert meta2["dropped"] == ["WonderCorp Psychometric Battery"]


# ---- FakeLM's own behavior (not via the DSPy adapter, for future orchestration-layer tests) ----

def test_fake_lm_queue_semantics() -> None:
    fake = FakeLM(responses=["a", "b"])
    assert fake("p1") == ["a"] and fake("p2") == ["b"]
    with pytest.raises(AssertionError, match="preset responses exhausted"):
        fake("p3")


def test_fake_lm_mapped_mode() -> None:
    fake = FakeLM(responses={"deadline": "x", "assessments": "y"})
    assert fake("please extract deadline here") == ["x"]
    assert fake("now assessments please") == ["y"]
    with pytest.raises(AssertionError, match="no matching rule"):
        fake("nothing matches")
