# APA — Application Pipeline Agent

**A governed job-application operations agent for Singapore — it monitors job
portals, enforces compliance rules mined from real incidents, extracts JD facts
with provenance, and audits every human decision. It never submits an
application on its own.**

📖 **[User Guide](USER_GUIDE.md)** — installation, LLM setup (local / cloud /
WorkBuddy), the end-to-end workflow with diagrams, command reference, and
troubleshooting.

Most open-source job-application agents compete on *automation*: discover more
jobs, tailor faster, submit hands-free. APA is built for the other side of the
problem — the **governance layer** that those pipelines usually lack:

- **Compliance rules mined from real incidents.** Every rule in the engine
  traces back to an actual failure (template residue across applications,
  duplicated paragraphs, unverified claims copied into checklists). The rule
  set is regression-locked against a golden corpus built from those incidents.
- **Portal health awareness.** A three-poll probe (status page, per-job URLs,
  maintenance notices) with a debounced state machine answers *"is the channel
  up"* — and, importantly, knows when it *cannot* answer *"is the job still
  open"* (that signal lives in the browser-rendered page, not in fetchable HTML).
- **Extraction with provenance.** JD facts are extracted with verbatim quotes
  located back in the source text by deterministic code — anything the model
  claims that cannot be located verbatim is dropped, not passed through.
- **Human-in-the-loop by design.** The agent drafts, checks, and monitors.
  A human makes every submission decision, records it with a reason, and the
  ledger is updated through a single audited channel.

> **Status:** personal project, under active development (D1–D13 of a staged
> plan; 287 tests). It has served real application final-reviews. See
> [Known limitations](#known-limitations) before relying on it.

---

## What it does

| Command | What you get |
|---|---|
| `apa status` | Current milestone and next step (numbers come from CI artifacts, never hand-written) |
| `apa compliance check <files...>` | Run the rule engine over your resume/cover letter — findings located to the exact line, hard vs warning severity |
| `apa probe run --kind all` | Probe job portals: status page, per-job URLs, maintenance notices → SQLite probe log |
| `apa probe evaluate` | Debounced state machine over probe history → channel states, outage intervals, affected-jobs triage (LOST / AT-RISK / RECOVERABLE) |
| `apa extract --jd <file>` | Extract deadline / requirements / assessment steps from a JD, each with a verbatim quote located in the source |
| `apa retrieve index` / `apa retrieve search --jd <file>` | **RAG over your own history**: index past submission packages (local embeddings + faiss), then retrieve the most similar past packages for a new JD — the agent reuses your own prior verdicts instead of starting cold |
| `apa review audit <review.md>` | Audit a third-party review opinion before you trust it — evidence anchors, actionability, boundary violations (8 dimensions) |
| `apa monitor --queue …` | Terminal live dashboard: channel health + submission queue (Rich) |
| `apa web --queue …` | HITL review console (browser): health cards, queue sorted by deadline, decision recording |

## The pipeline

```
ingest ──► triage ──► compliance ──► draft ──► adapt ──► ⏸ HITL ──► submit ──► audit
                                        ▲                                  │
                                        └────────── probe/extract ─────────┘
```

The compliance engine has three rule families:

1. **Inherited red lines** (12 rules) ported verbatim from a working
   line-checker, regression-locked to identical output on a 10-letter corpus;
2. **Accident rules** (A1–A5), each reproducing a numbered real incident on a
   golden before/after pair — and each verified *not* to fire on clean copies;
3. **Channel rules** (deadline buffer for government postings, portal-health
   gate that fails closed, channel-divergence guard).

## Quick start

```bash
# Python 3.11 (conda recommended)
conda create -n apa python=3.11 && conda activate apa
pip install -e ".[dev,eval]"

# 1. Compliance-check a cover letter against the incident-mined rules
apa compliance check CoverLetter.md

# 2. Health-check your target job portals (writes a local SQLite probe log)
apa probe run --kind all
apa probe evaluate

# 3. Extract structured facts from a JD (deadline / requirements / assessments)
apa extract --jd job_posting.md

# 4. Audit a review opinion someone handed you
apa review audit review_opinion.md --fail-on high

# 5. Watch it live / review in the browser
apa monitor --queue data/submission_queue.json
apa web --queue data/submission_queue.json --port 8501   # binds 127.0.0.1 only
```

**Optional local LLM** (no cloud, no API key): install
[llama.cpp](https://github.com/ggml-org/llama.cpp) and serve a small GGUF
model, then point APA at it:

```bash
llama-server -m qwen2.5-1.5b-instruct-q4_k_m.gguf --port 8080 -ngl 99
# APA auto-detects http://127.0.0.1:8080/v1 as its "local" backend
```

**Backend selection** (user-configurable priority chain):

| Backend | Env | Data boundary | Role |
|---|---|---|---|
| WorkBuddy desktop models | `APA_LLM_BACKEND=workbuddy` | egress (cloud transit) | debugging with strong models |
| Cloud-private (Bedrock In-Region / Azure / Vertex) | `APA_LLM_BACKEND=cloud` + `APA_ALLOW_CLOUD_LLM=1` | egress | semantic judging, list extraction |
| Local llama.cpp | `APA_LLM_BACKEND=local` (default) | **stays on your machine** | single-value extraction, offline |

> Real application materials must pass a PII gate before any egress backend is
> used (Presidio integration is the next milestone). Only the local backend is
> truly on-premises.

## Testing philosophy

Layered, after the industry standard (mock for logic, real calls for effect —
never interchangeable):

- **Unit/CI** (every commit, ~30 s): fake LLM responses, replay judges, fake
  transports — zero network, deterministic assertions;
- **Assessment** (low frequency, milestones): real model runs against a golden
  corpus — DeepEval semantic gates (a corrupted case must score below
  threshold, a clean control must pass), batch consistency tables over
  historical JDs;
- **Regression locks**: the inherited rule set must produce byte-identical
  findings to the live line-checker it was ported from; every numbered
  incident must reproduce; clean controls must not fire.

Run everything:

```bash
pytest -q          # 287 tests
```

## Data sovereignty

- Your job data (`data/`, probe targets, extraction reviews) stays on disk and
  is git-ignored;
- The review console binds to `127.0.0.1` only and opens the ledger read-only;
- Any egress backend requires an explicit double opt-in, and real materials
  are gated behind PII sanitization;
- The web console writes human decisions to an append-only local audit log —
  it never writes to your job ledger directly.

## Channel profiles

`channel_profiles/` documents what we know about each Singapore application
channel (Careers@Gov/HRP, MyCareersFuture, Workday tenants, JobStreet):
known form fields, limit-discovery triage (DOM / measure / unverified),
attachment slots, and operational risks. **Every profile carries the same
warning: limits vary per job — verify per form, every time.**

## Known limitations

- The bundled 1.5B local model reliably extracts **single-value** JD facts
  (deadlines) but **not list-shaped** ones (requirements, assessments) — that
  is a measured capability boundary, and the reason for the backend chain;
- Careers@Gov renders its closing-date label client-side: fetchable HTML
  cannot answer "is this job still open" (a user-visible rendered-state
  signal — headless probes are structurally insufficient there);
- The inherited rule family's false-positive rate on a 44-document production
  scan is above the week-4 exit bar; tightening it is the current main push.

## Public release checklist

See [SANITIZATION.md](SANITIZATION.md) — generated by `scripts/sync_public.py`,
which is the **only** path from the private working repo into this one
(allow-list exporter: real runs, real golden sets, and personal data are
structurally excluded).

## License

[MIT](LICENSE) — free to use, fork, and adapt. Attribution appreciated but not
required.

## Disclaimer

Personal, non-commercial project. Not affiliated with any job board or the
Government of Singapore. Users are responsible for complying with the terms
of service of any portal they interact with, and — always — for every click
of the final Submit button.
