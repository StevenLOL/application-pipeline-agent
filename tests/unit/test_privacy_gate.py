"""R3: privacy gate tests — regex detection / blocking semantics / mask /
**canary leak tests**.

Canary discipline (defense-in-depth layering + the JD's "privacy leakage
checks"): not "trust that the block exists", but **inject fake PII through the
real egress path and assert the fake client receives zero canary**.
"""

from __future__ import annotations

import pytest

from apa.llm import chat
from apa.llm.client import LLMSettings, PresidioGateRequired
from apa.privacy import PIIGate

CANAry_MATERIAL = (
    "Dear hiring team, I am S1234567D, reachable at canary@example.com "
    "or +65 9123 4567. My address: 140 Hill Street, Singapore."
)


# ---- regex detection layer ----

class TestScan:
    def test_nric_detected(self):
        f = PIIGate(use_presidio=False).scan("my id is S1234567D here")
        kinds = [x.kind for x in f]
        assert "SG_NRIC" in kinds

    def test_phone_and_email_detected(self):
        f = PIIGate(use_presidio=False).scan("call +65 9123 4567 or mail a@b.com")
        kinds = {x.kind for x in f}
        assert "SG_PHONE" in kinds and "EMAIL" in kinds

    def test_clean_text_no_findings(self):
        assert PIIGate(use_presidio=False).scan(
            "Senior AI Engineer with RAG and agentic workflow experience.") == []

    def test_mask_replaces_pii(self):
        masked = PIIGate(use_presidio=False).mask(CANAry_MATERIAL)
        assert "S1234567D" not in masked and "<SG_NRIC>" in masked
        assert "canary@example.com" not in masked and "<EMAIL>" in masked


# ---- blocking semantics ----

class TestBlocking:
    def test_assert_safe_raises_on_pii(self):
        with pytest.raises(PresidioGateRequired, match="PII finding"):
            PIIGate(use_presidio=False).assert_safe("id S1234567D", label="cover letter")

    def test_assert_safe_passes_clean(self):
        PIIGate(use_presidio=False).assert_safe("RAG and agentic workflows.")


# ---- canary leak tests (core: inject → real egress path → assert zero leak) ----

class SpyClient:
    """Fake egress client that captures what is actually sent."""

    def __init__(self):
        self.sent: list[str] = []

    class _Completions:
        def __init__(self, spy):
            self._spy = spy

        def create(self, **kwargs):
            self._spy.sent.append(str(kwargs["messages"]))
            raise AssertionError("outbound call must be blocked before reaching the client")

    chat = None  # assembled in __init__

    def __post_init__(self):
        self.chat = self.Completions(self)


def test_canary_never_reaches_egress_client():
    """Canary material (with NRIC/email/phone) goes through the egress chat →
    zero client calls, zero leak."""
    spy = SpyClient()
    spy.chat = SpyClient._Completions(spy)
    settings = LLMSettings(
        backend="workbuddy", base_url="http://127.0.0.1:8787/v1",
        api_key="k", model="auto", cloud_allowed=False,
    )

    with pytest.raises(PresidioGateRequired):
        chat(CANAry_MATERIAL, settings=settings, material_cleared=False,
             _client=spy)  # inject the spy: if the gate fails, the client receives the canary and raises AssertionError

    assert spy.sent == [], "canary leak: the egress client received PII-bearing material"
