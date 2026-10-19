"""Data sovereignty status tests.

**Core discipline (established after the 2026-10-19 follow-up question)**:

In cloud LLM mode, **never display "data never leaves this machine"**. In that
mode the sentence is a **well-meaning lie** — far more dangerous than stating
plainly "material goes to the cloud, but PII is stopped by the gate".

Code basis: ``llm/client.py:47`` (workbuddy/cloud egress at the same tier),
``:61`` (is_egress), ``:219-222`` (egress and not declared cleared →
mandatory PII gate, raise on detection).
"""

from __future__ import annotations

import pytest

from apa.sovereignty import (SovereigntyState, SovereigntyStatus, current_status,
                             material_fingerprint, status_after_send)


def status(backend: str, **kw) -> SovereigntyStatus:
    return SovereigntyStatus(
        state=kw.pop("state"),
        backend=backend, model=kw.pop("model", "m"), **kw)


class TestLabels:
    def test_local_says_data_stays_here(self):
        s = SovereigntyStatus(SovereigntyState.LOCAL, "local")
        assert "never leaves this machine" in s.label
        assert s.color == "positive"

    def test_egress_never_claims_data_stays_here(self):
        """**Core discipline**: in the egress state the copy must not claim
        "data never leaves this machine".

        The 2026-10-19 follow-up asked whether "data never leaves this
        machine" covers not calling cloud LLMs — the original design still
        displayed this sentence in cloud mode, which is a **well-meaning
        lie**. UI copy is always English (standing design decision).
        """
        s = SovereigntyStatus(SovereigntyState.EGRESS, "cloud", model="claude")
        assert "never leaves this machine" not in s.label
        assert "Cloud" in s.label and s.color == "warning"

    def test_egress_is_detailable(self):
        """In the egress state it must be expandable for details — no vague
        promises."""
        assert SovereigntyStatus(SovereigntyState.EGRESS, "cloud").detailable

    def test_local_is_not_detailable(self):
        assert not SovereigntyStatus(SovereigntyState.LOCAL, "local").detailable

    def test_blocked_is_red_and_not_detailable(self):
        s = SovereigntyStatus(SovereigntyState.BLOCKED, "cloud", gate_result="praised")
        assert s.color == "negative" and not s.detailable
        assert "blocked" in s.label.lower()

    def test_labels_are_english_only(self):
        """UI and code are always English (standing design decision: the UI
        and UX are all English)."""
        for st in SovereigntyState:
            label = SovereigntyStatus(st, "x").label
            assert not any(ord(c) > 0x2E80 for c in label), f"CJK in {st}: {label}"

    def test_every_state_has_a_label(self):
        for st in SovereigntyState:
            assert SovereigntyStatus(st, "x").label


class TestDetailLines:
    def test_detail_shows_backend_model_and_gate(self):
        s = SovereigntyStatus(SovereigntyState.EGRESS, "cloud", model="claude-sonnet-5",
                              cloud_allowed=True, gate_result="passed")
        text = "\n".join(s.detail_lines())
        assert "cloud" in text and "claude-sonnet-5" in text
        assert "passed" in text

    def test_detail_never_contains_the_material_itself(self, monkeypatch):
        """The audit stores only fingerprints, never full text — otherwise it
        amounts to copying PII into a second file."""
        class S:
            backend = "cloud"
            model = "claude"
            cloud_allowed = True
            is_egress = True

        monkeypatch.setattr("apa.llm.client.load_settings", lambda **k: S())
        s = status_after_send("my email is a@b.com and NRIC S1234567D")
        assert s.last_material_fp
        joined = "\n".join(s.detail_lines())
        assert "a@b.com" not in joined and "S1234567D" not in joined
        assert s.last_material_fp in joined


class TestFingerprint:
    def test_stable_and_short(self):
        a = material_fingerprint("same text")
        assert a == material_fingerprint("same text")
        assert len(a) == 12

    def test_different_text_different_fingerprint(self):
        assert material_fingerprint("a") != material_fingerprint("b")

    def test_handles_empty(self):
        assert material_fingerprint("") == material_fingerprint("")


class TestCurrentStatus:
    def test_local_backend_yields_local(self, monkeypatch):
        class S:
            backend = "local"
            model = "qwen2.5-1.5b-instruct"
            cloud_allowed = False
            is_egress = False

        monkeypatch.setattr("apa.llm.client.load_settings", lambda **k: S())
        s = current_status()
        assert s.state is SovereigntyState.LOCAL
        assert s.gate_result == "not_needed"

    @pytest.mark.parametrize("backend", ["cloud", "workbuddy"])
    def test_egress_backends_yield_egress_with_gate(self, monkeypatch, backend):
        class S:
            model = "some-model"
            cloud_allowed = True
            is_egress = True

        S.backend = backend
        monkeypatch.setattr("apa.llm.client.load_settings", lambda **k: S())
        s = current_status()
        assert s.state is SovereigntyState.EGRESS
        assert s.gate_result == "passed"
        assert "数据不出机器" not in s.label

    def test_unconfigured_is_not_a_crash(self, monkeypatch):
        def boom(**k):
            raise RuntimeError("no endpoint")

        monkeypatch.setattr("apa.llm.client.load_settings", boom)
        s = current_status()
        assert s.state is SovereigntyState.UNKNOWN
        assert "not configured" in s.note


class TestStatusAfterSend:
    def test_local_send_records_no_fingerprint(self):
        """Local processing should not produce an "egress material
        fingerprint" — that would make people think it was sent out."""
        class S:
            backend = "local"
            model = "qwen"
            cloud_allowed = False
            is_egress = False

        import apa.llm.client as lc
        orig = lc.load_settings
        lc.load_settings = lambda **k: S()
        try:
            s = status_after_send("some resume text")
        finally:
            lc.load_settings = orig
        assert s.state is SovereigntyState.LOCAL and s.last_material_fp is None

    def test_egress_send_records_fingerprint(self, monkeypatch):
        class S:
            backend = "cloud"
            model = "claude"
            cloud_allowed = True
            is_egress = True

        monkeypatch.setattr("apa.llm.client.load_settings", lambda **k: S())
        s = status_after_send("resume body", cleared=False)
        assert s.state is SovereigntyState.EGRESS
        assert s.last_material_fp == material_fingerprint("resume body")
        assert s.gate_result == "passed"
