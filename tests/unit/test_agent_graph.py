"""Agent loop end-to-end tests (Fake components — zero models, zero network,
zero external services).

What is under test is the **orchestration mechanism**: node order, interrupt
semantics (pause/resume/validate), SqliteSaver persistence, audit persistence
— model quality belongs to the evaluation layer (defense-in-depth layering).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.types import Command

from apa.agent import Components, build_graph, default_checkpointer
from apa.extract import JDExtractor
from apa.llm import FakeLM, FakePredictor
from apa.retrieve import FakeEmbedder, Retriever


JD = ("Senior AI Engineer. RAG pipelines and LangGraph agentic workflows. "
      "Closing on 22 Oct 2026.")


class _FakeExtractor:
    """Minimal stand-in for extract_with_consistency (returns deterministic
    results)."""

    def __init__(self, deadline: str, needs_human: bool = False):
        self.deadline = deadline
        self.needs_human = needs_human
        self.calls = 0

    def extract_with_consistency(self, jd_text: str, jd_ref: str = "", n: int = 5):
        self.calls += 1

        class R:
            values = {"deadline": self.deadline}
            schema_errors: dict = {}
            needs_human = self.needs_human
            consistency: dict = {}

        return R()


class _FakeRetriever:
    def __init__(self):
        self.calls = 0

    def search(self, query: str, k: int = 5):
        self.calls += 1
        return [type("H", (), {"doc_id": "GOV-A", "score": 0.8,
                               "meta": {"title": "AI Engineer"},
                               "source_path": "/pkg/A/JD.md"})()]


@pytest.fixture
def comp(tmp_path):
    return Components(
        extractor=_FakeExtractor("2026-10-22"),
        retriever=_FakeRetriever(),
        audit_path=tmp_path / "agent_runs.jsonl",
    )


def _graph(comp, tmp_path):
    return build_graph(comp, read_jd=lambda jd_id: JD,
                       checkpointer=default_checkpointer(tmp_path / "ckpt.db"))


CONFIG = {"configurable": {"thread_id": "t-1"}}


def test_full_loop_with_human_resume(comp, tmp_path):
    """Full loop: invoke to the HITL interrupt -> Command(resume) -> audit."""
    g = _graph(comp, tmp_path)
    result = g.invoke({"jd_id": "GOV-A", "jd_path": "fake.md", "jd_text": ""}, config=CONFIG)

    # the interrupt is at review: state already has the
    # extract/retrieve/compliance/triage products
    assert result["deadline"] == "2026-10-22"
    assert result["similar"][0]["doc_id"] == "GOV-A"
    assert result["compliance_findings"] == []  # pure JD text, no checklist
    assert result["triage"]["recommendation"] in ("proceed-with-caution", "act-now", "skip")
    assert "human_decision" not in result or not result.get("human_decision")
    assert "__interrupt__" in result  # langgraph interrupt marker

    # human ruling (approve)
    final = g.invoke(Command(resume={"action": "approve", "reason": "deadline ok"}),
                     config=CONFIG)
    assert final["human_decision"]["action"] == "approve"
    assert final["audit_ref"].endswith("agent_runs.jsonl")
    lines = Path(comp.audit_path).read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["jd_id"] == "GOV-A"
    assert "triage" in entry and "compliance_findings" in entry  # new audit fields


def test_waive_requires_reason_at_resume(comp, tmp_path):
    """waive with no reason → resume rejected (validation lives inside the
    graph; the human must supply a reason)."""
    g = _graph(comp, tmp_path)
    g.invoke({"jd_id": "GOV-A", "jd_path": "fake.md", "jd_text": ""}, config=CONFIG)
    with pytest.raises(ValueError, match="waive requires a reason"):
        g.invoke(Command(resume={"action": "waive", "reason": ""}), config=CONFIG)


def test_state_persists_across_graph_instances(comp, tmp_path):
    """SqliteSaver: switching to another graph instance (simulating a process
    restart) still resumes from the interrupt point."""
    g1 = _graph(comp, tmp_path)
    g1.invoke({"jd_id": "GOV-A", "jd_path": "fake.md", "jd_text": ""}, config=CONFIG)
    g2 = _graph(comp, tmp_path)  # a new instance (same checkpointer file)
    final = g2.invoke(Command(resume={"action": "defer", "reason": "wait"}),
                      config=CONFIG)
    assert final["human_decision"]["action"] == "defer"


def test_extract_needs_human_still_reaches_review(comp, tmp_path):
    """Low extraction confidence does not block — proceed to review as usual,
    with the human ruling on the full information (the HITL point in the
    design docs)."""
    comp.extractor = _FakeExtractor("NOT_PUBLISHED", needs_human=True)
    g = _graph(comp, tmp_path)
    result = g.invoke({"jd_id": "GOV-A", "jd_path": "fake.md", "jd_text": ""}, config=CONFIG)
    assert result["deadline"] == "NOT_PUBLISHED"
    assert result["needs_human"] is True
    assert "__interrupt__" in result


def test_nodes_idempotent_rerun(comp, tmp_path):
    """On resume the nodes rerun from the start (a langgraph production
    essential) — the extractor being called again does not crash."""
    g = _graph(comp, tmp_path)
    g.invoke({"jd_id": "GOV-A", "jd_path": "fake.md", "jd_text": ""}, config=CONFIG)
    calls_before = comp.extractor.calls
    g.invoke(Command(resume={"action": "approve", "reason": "ok"}), config=CONFIG)
    assert comp.extractor.calls >= calls_before  # rerun does not crash (idempotent: pure computation)
