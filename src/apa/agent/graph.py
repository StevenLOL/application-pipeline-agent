"""Agent loop v0 (R2 / ROADMAP §3.2): a LangGraph-orchestrated end-to-end JD processing loop.

This is not a demo — it synthesizes the 4–6 manual steps of the real weekly submission workflow
(extract → retrieve → human adjudication → audit) into a single command,
with state persisted via SqliteSaver (recoverable after a mid-run disconnection), and HITL waiting
on a person via interrupt().

Graph structure (the triage-exit HITL point from the design docs):

    ingest ──► extract ──► retrieve ──► review ⏸(interrupt) ──► audit
                                    the human sees: deadline + similar historical packages + consistency
                                    the human gives: approve / waive(+reason)

Discipline:
- All capability components (extractor/retriever) are constructor-injected — tests use Fakes, zero models, zero network;
- interrupt depends on the checkpointer (per the ops runbook: MemorySaver is banned in production);
- the audit node is append-only (data/agent_runs.jsonl) — it does not write to the ledger;
- nodes are idempotent (on resume they re-run from the start of the node — a langgraph production point
  cited in the ops runbook).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

AUDIT_LOG = Path("data") / "agent_runs.jsonl"


class AgentState(TypedDict, total=False):
    jd_id: str
    jd_path: str
    jd_text: str
    deadline: str
    deadline_schema_error: str
    similar: list[dict[str, Any]]
    compliance_findings: list[dict[str, Any]]
    triage: dict[str, Any]
    needs_human: bool
    human_decision: dict[str, Any]
    audit_ref: str


@dataclass
class Components:
    """Capability components (constructor-injected — tests use Fakes, production uses real implementations)."""

    extractor: Any                      # apa.extract.JDExtractor
    retriever: Any                      # apa.retrieve.Retriever
    audit_path: Path = field(default_factory=lambda: AUDIT_LOG)
    extract_runs: int = 5


def node_ingest(state: AgentState, *, read_jd: Callable[[str], str]) -> dict:
    """Read the JD file → jd_text (idempotent: pure read)."""
    return {"jd_text": read_jd(state["jd_path"])}


def node_extract(state: AgentState, comp: Components) -> dict:
    """Extract the deadline (5-run agreement; idempotent: pure computation)."""
    result = comp.extractor.extract_with_consistency(
        state["jd_text"], jd_ref=state["jd_id"], n=comp.extract_runs)
    return {
        "deadline": result.values.get("deadline"),
        "deadline_schema_error": result.schema_errors.get("deadline", ""),
        "needs_human": result.needs_human,
    }


def node_retrieve(state: AgentState, comp: Components) -> dict:
    """Retrieve similar historical packages (idempotent: pure read)."""
    hits = comp.retriever.search(state["jd_text"], k=5)
    return {"similar": [
        {"doc_id": h.doc_id, "score": round(h.score, 3),
         "title": h.meta.get("title", ""), "path": h.source_path}
        for h in hits
    ]}


def node_compliance(state: AgentState) -> dict:
    """A4 claim-verification on the JD document (incident-4 guardline).

    Applies when the input is a full JD.md (checklist + JD body sections) -
    the exact accident-4 shape: a checklist action item whose employer-process
    entity is absent from the JD body. Pure JD body text (no checklist
    section) -> not applicable, empty findings.
    """
    from ..compliance.rules.accidents import claim_verification_check

    findings = claim_verification_check(state["jd_text"])
    return {"compliance_findings": [
        {"line": ln, "note": note} for ln, note in findings
    ]}


def node_triage(state: AgentState) -> dict:
    """Deterministic recommendation from retrieval + deadline + compliance.

    No ML - a transparent heuristic the human can audit:
    - top similarity >= 0.75 to a past package -> "prior art exists" signal
    - compliance findings > 0 -> "fix before drafting" signal
    - deadline within 3 days -> urgency signal
    """
    similar = state.get("similar", [])
    top = similar[0]["score"] if similar else 0.0
    findings = state.get("compliance_findings", [])
    deadline = state.get("deadline")
    signals: list[str] = []
    urgent = False
    if top >= 0.75:
        signals.append(f"similar past package exists (top {top})")
    if findings:
        signals.append(f"{len(findings)} compliance finding(s) need fixing before drafting")
    if deadline and deadline != "NOT_PUBLISHED":
        try:
            days = (datetime.fromisoformat(deadline).date()
                    - datetime.now(timezone.utc).date()).days
            if days <= 3:
                urgent = True
                signals.append(f"deadline in {days} day(s)")
        except ValueError:
            pass
    recommendation = (
        "skip" if not signals else
        ("act-now" if urgent and not findings else "proceed-with-caution")
    )
    return {"triage": {"recommendation": recommendation, "signals": signals,
                       "top_similarity": top}}


def node_review(state: AgentState) -> dict:
    """⏸ HITL interrupt: the human sees the full processing report
    (deadline + similar packages + compliance findings + recommendation)
    and returns approve/waive + reason (Command(resume=...) to continue)."""
    decision = interrupt({
        "jd_id": state["jd_id"],
        "deadline": state.get("deadline"),
        "similar": state.get("similar", []),
        "compliance_findings": state.get("compliance_findings", []),
        "triage": state.get("triage", {}),
        "prompt": "approve / waive (+reason) / defer — the decision enters the audit",
    })
    if not isinstance(decision, dict) or decision.get("action") not in ("approve", "waive", "defer"):
        raise ValueError(f"invalid human decision: {decision!r}")
    if decision["action"] == "waive" and not str(decision.get("reason", "")).strip():
        raise ValueError("waive requires a reason")
    return {"human_decision": {
        "action": decision["action"],
        "reason": str(decision.get("reason", "")).strip(),
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decided_by": "human",
    }}


def node_audit(state: AgentState, comp: Components) -> dict:
    """Audit trail (append-only jsonl; resume semantics keep one final entry
    per thread)."""
    entry = {
        "ts": state["human_decision"]["decided_at"],
        "thread": state["jd_id"],
        "jd_id": state["jd_id"],
        "deadline": state.get("deadline"),
        "similar_top": (state.get("similar") or [{}])[0].get("doc_id"),
        "triage": state.get("triage", {}).get("recommendation"),
        "compliance_findings": len(state.get("compliance_findings", [])),
        **state["human_decision"],
    }
    comp.audit_path.parent.mkdir(parents=True, exist_ok=True)
    comp.audit_path.open("a", encoding="utf-8").write(
        json.dumps(entry, ensure_ascii=False) + "\n")
    return {"audit_ref": str(comp.audit_path)}


def build_graph(comp: Components, read_jd: Callable[[str], str], checkpointer: Any) -> Any:
    g = StateGraph(AgentState)
    g.add_node("ingest", lambda s: node_ingest(s, read_jd=read_jd))
    g.add_node("extract", lambda s: node_extract(s, comp))
    g.add_node("retrieve", lambda s: node_retrieve(s, comp))
    g.add_node("compliance", lambda s: node_compliance(s))
    g.add_node("triage", lambda s: node_triage(s))
    g.add_node("review", lambda s: node_review(s))
    g.add_node("audit", lambda s: node_audit(s, comp))
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "extract")
    g.add_edge("extract", "retrieve")
    g.add_edge("retrieve", "compliance")
    g.add_edge("compliance", "triage")
    g.add_edge("triage", "review")
    g.add_edge("review", "audit")
    g.add_edge("audit", END)
    return g.compile(checkpointer=checkpointer)


def make_components(extractor: Any, retriever: Any,
                    audit_path: Path = AUDIT_LOG) -> Components:
    return Components(extractor=extractor, retriever=retriever, audit_path=audit_path)


def default_checkpointer(db_path: Path = Path("data") / "checkpoints.db") -> SqliteSaver:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = __import__("sqlite3").connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)
