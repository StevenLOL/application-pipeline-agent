"""Main orchestration: LangGraph state machine (ingest -> triage -> compliance ->
draft -> channel adaptation -> HITL -> write-back -> audit).

HITL pauses inside a node via interrupt() and resumes with Command(resume=...);
it must be paired with a SqliteSaver checkpointer, and nodes must be idempotent
(on resume they re-run from the node head). The 5 HITL points are defined in
the product-design doc. Assembly happens once the F components are ready
(weeks 3-4).
"""
