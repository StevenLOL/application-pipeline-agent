# Roadmap — from CLI toolkit to a complete, working agent

> This project is built by its own target user: a job seeker in Singapore,
> currently between roles, using it on real applications every week. Every
> capability below must be **useful first** — the agent examples exist because
> the pipeline needs them, never as demos for their own sake.

## 1. What "a complete agent" means here

An end-to-end loop over a *real* submission, not a synthetic demo:

```
new JD URL
  └─► ingest (fetch + archive, provenance recorded)
       └─► extract (LLM: deadline/requirements/assessments, quotes located in source)
            └─► retrieve (RAG over past submission packages: similar JDs, prior verdicts, lessons)
                 └─► triage (score + classify, calibrated by past decisions)
                      └─► compliance (incident-mined rules, line-level findings)
                           └─► draft (channel-aware, per-form limits)
                                └─► ⏸ HITL (human approves / waives with reason)
                                     └─► monitor (portal health, debounced)
                                          └─► audit (append-only, every decision)
```

Each stage is a **tool the agent calls**; the orchestration layer (LangGraph,
D12+) turns the chain into an agentic workflow with checkpointed state and a
human interrupt — the industry-standard pattern for production agents.

## 2. JD requirement → project coverage map

| JD requirement | Where it lives | Status |
|---|---|---|
| LLM applications | `src/apa/llm/` dual-backend client (local llama.cpp / cloud-private / WorkBuddy), `src/apa/extract/` DSPy signatures | ✅ shipped |
| **RAG** | `src/apa/retrieve.py` — retrieval over the historical submission corpus feeding triage and drafting | ✅ **shipped (R1)** |
| **Embeddings + vector search** | same module: bge-small-en local model (on disk, no API) + faiss IndexFlatIP | ✅ **shipped (R1)** |
| **Tool / function calling** | MCP server exposing ledger/query/update tools (`src/apa/mcp_registry.py`); agent tools = pipeline stages | ✅ shipped (agent-loop wiring next) |
| **Agentic workflows** | LangGraph orchestration: ingest → extract → retrieve → **compliance** → **triage** → `interrupt()` HITL → audit, with SqliteSaver | ✅ **shipped (R2.5)** |
| Experimental design / measurement | bootstrap CIs on false-positive rates; 5-run consistency; paired time comparisons | ✅ shipped |
| Error analysis / evaluation methodology | golden corpus from numbered incidents; DeepEval semantic gates; batch consistency tables | ✅ shipped |
| **Fine-tuning** | LoRA on 0.5B/1.5B via LLaMA-Factory (GPU-constrained) | planned (conditional) |
| Prompt optimization | versioned prompts + saved DSPy programs | planned |
| Applied NLP: extraction | F3 with quote-provenance + calibration by consistency | ✅ shipped |
| Applied NLP: reliability | needs_human gating (schema-fail or low consistency → human queue) | ✅ shipped |
| **Responsible AI: privacy leakage** | Presidio blocking gate before any egress LLM call; canary leakage tests | 🚧 next milestone |
| **Responsible AI: bias testing** | paired counterfactual tests (swap employer/agency names → findings must not flip) | planned |
| **Responsible AI: audit artifacts** | append-only decision logs, versioned CI reports, provenance in every extraction | ✅ shipped |

## 3. Milestones (in build order)

0. **R0 — Profile, preferences & discovery ingest (next)**: `profile.yaml`
   (single source of truth: identity/experience/skills/metrics-ledger with
   sources), `preferences` (keywords/min-score/channels/deadline buffer), and
   the **URL-list discovery ingest** (paste public-posting URLs from the free
   portals; APA ingests each into the pipeline). Rationale: the user journey
   starts *before* a JD is in hand — the three big OSS competitors all ship
   discovery first (profile.yaml + discover.py is the standard pattern).
   Portal-search automation stays optional and ToS-bounded; the URL-list path
   ships with zero scraping.
1. **R1 — Retrieval v0 ✅**: index the historical corpus (22+ packages,
   647-entry government JD master list) with local embeddings + faiss; query a
   new JD → top-k similar past packages with scores. *Useful immediately: the
   agent reuses your own prior verdicts and lessons instead of starting cold.*
2. **R2 — Agent loop v0 ✅**: LangGraph graph wiring ingest → extract →
   compliance → triage as tool-calling nodes with an `interrupt()` before
   anything that writes; SqliteSaver so state survives restarts.
3. **R3 — Privacy gate ✅**: Presidio blocking wrapper + canary leakage tests
   (inject fake PII, assert zero egress).
4. **R4 — Counterfactual bias tests ✅**: paired inputs differing only in
   protected attributes / employer names; findings must be invariant.
5. **R5 — Fine-tuning (conditional)**: LoRA on small models only if the
   extraction consistency table shows a repeatable win.
6. **R6 — Post-submission lifecycle** (designed, input from the author):
   after Submit, the journey continues and the agent should follow it:
   - **Confirmation collection**: read-only email auditing (IMAP/Gmail
     read-only, the F12 design's "proposal-tier" posture) detects application
     confirmations and rejections; a WhatsApp/telegram notification forwarder
     is the mobile channel. Everything read-only until the human decides.
   - **Interview scheduling intake**: detect interview invitations and draft
     the calendar entry + preparation checklist for human confirmation.
   - **Interview preparation**: per-employer brief assembled from the
     archived package (JD, your submitted documents, the employer's public
     surface) — study material, not a chatbot.
   - Design constraint inherited from F12: read-only collection is
     proposal-tier (the human confirms every state change in the ledger);
     no auto-reply, no auto-scheduling on the human's behalf.

## 4. Non-goals

- No autonomous submission. The human clicks Submit — always.
- No demo-only features: anything that cannot serve a real application this
  week is deferred.
- No cloud dependency for the core loop: local llama.cpp + local embeddings
  keep the pipeline runnable offline.
