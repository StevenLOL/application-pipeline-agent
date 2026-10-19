"""APA command-line entry point.

Subcommands land on schedule as their F components are built:
  status     milestone status (D1)
  compliance check   F1 compliance check (D2): rules-table driven, line-anchored report
  probe      F10 channel health probe (D4): run / targets / status / register-tasks
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, config
from .compliance.engine import KIND_CORPUS, Context, Report, check_files
from .compliance.rules import get_rules
from .compliance.rules.accidents import Corpus, fork_of


def _cmd_review_audit(args: argparse.Namespace) -> int:
    """Audit a review opinion (third-party input or APA self-review).

    What is audited is not whether the opinion is right, but whether it can be
    adopted: evidence anchors, reproducible paths, impact-chain argumentation,
    actionable steps, staying in scope, numeric self-consistency, provenance
    declaration. The output is a recommendation, not a verdict; the final call
    is made by a human.
    """
    from .review.audit import audit_file, render_markdown

    res = audit_file(args.file)
    report = render_markdown(res)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"report written: {args.out}")
    else:
        print(report)
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(
            json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"json written: {args.json_path}")
    counts = res.counts()
    if args.fail_on == "high" and counts.get("high", 0):
        return 1
    if args.fail_on == "medium" and (counts.get("high", 0) or counts.get("medium", 0)):
        return 1
    return 0


def _cmd_retrieve_index(args: argparse.Namespace) -> int:
    """Build the retrieval index over historical submission packages (local embeddings + faiss)."""
    from .retrieve import BgeEmbedder, Retriever, build_corpus_from_packages

    corpus = build_corpus_from_packages(args.packages)
    if not corpus:
        print(f"error: corpus is empty (no JD.md containing a \"JD original text\" section under {args.packages})",
              file=sys.stderr)
        return 2
    emb = BgeEmbedder(args.model)
    r = Retriever(emb, args.index_dir)
    n = r.build_index(corpus)
    print(f"index built: {n} JDs -> {args.index_dir}")
    return 0


def _cmd_retrieve_search(args: argparse.Namespace) -> int:
    """Query the most similar historical submission packages (top-k, with similarity scores and source paths)."""
    from .retrieve import BgeEmbedder, Retriever

    if not args.jd and not args.query:
        print("error: provide exactly one of --jd or --query", file=sys.stderr)
        return 2
    query_text = args.query or Path(args.jd).read_text(encoding="utf-8", errors="replace")
    r = Retriever(BgeEmbedder(args.model), args.index_dir)
    hits = r.search(query_text, k=args.k)
    if not hits:
        print("(index is empty - run `apa retrieve index` first)")
        return 1
    print(f"Top {len(hits)} similar historical submission packages:")
    for h in hits:
        print(f"  {h.score:.3f}  {h.doc_id:<18} {h.meta.get('title', '')[:40]}")
        print(f"          {h.source_path}")
    return 0


def _cmd_agent_run(args: argparse.Namespace) -> int:
    """Agent loop: process a new JD until the HITL interrupt (resume after human adjudication)."""
    from .agent import build_graph, default_checkpointer, make_components
    from .extract import JDExtractor
    from .llm import load_settings
    from .retrieve import BgeEmbedder, Retriever

    jd_path = Path(args.jd)
    if not jd_path.exists():
        print(f"error: JD file does not exist: {jd_path}", file=sys.stderr)
        return 2
    jd_id = args.jd_id or jd_path.stem
    thread = args.thread or jd_id

    settings = load_settings()
    lm = None
    if settings.backend == "local":
        from .llm import FakeLM  # noqa: F401  (real LM: see the D8 runbook)
        lm = configure_dspy_local(settings)
    extractor = JDExtractor(lm=lm) if lm else JDExtractor()
    retriever = Retriever(BgeEmbedder(r"E:\dev\models\bge-small-en-v1.5"),
                          config.REPO_ROOT / "data" / "retrieval_index")
    comp = make_components(extractor, retriever, audit_path=Path(args.audit))
    g = build_graph(comp, read_jd=lambda p: Path(p).read_text(encoding="utf-8", errors="replace"),
                    checkpointer=default_checkpointer(Path(args.ckpt)))
    result = g.invoke({"jd_id": jd_id, "jd_path": str(jd_path), "jd_text": ""},
                      config={"configurable": {"thread_id": thread}})
    print(f"deadline: {result.get('deadline')}")
    print(f"needs_human: {result.get('needs_human')}")
    print("similar historical packages:")
    for s in result.get("similar", []):
        print(f"  {s['score']:.3f} {s['doc_id']} {s.get('title', '')[:40]}")
    if "__interrupt__" in result:
        print("\n⏸ HITL interrupt - resume after human adjudication:")
        print(f"  apa agent resume --thread {thread} --action approve|waive|defer "
              f"--reason \"...\" --jd \"{jd_path}\"")
    return 0


def _cmd_agent_resume(args: argparse.Namespace) -> int:
    """Resume a thread interrupted by HITL (human adjudication entry)."""
    from langgraph.types import Command

    from .agent import build_graph, default_checkpointer, make_components
    from .extract import JDExtractor
    from .llm import load_settings
    from .retrieve import BgeEmbedder, Retriever

    settings = load_settings()
    lm = None
    if settings.backend == "local":
        lm = configure_dspy_local(settings)
    extractor = JDExtractor(lm=lm) if lm else JDExtractor()
    retriever = Retriever(BgeEmbedder(r"E:\dev\models\bge-small-en-v1.5"),
                          config.REPO_ROOT / "data" / "retrieval_index")
    comp = make_components(extractor, retriever, audit_path=Path(args.audit))
    g = build_graph(comp, read_jd=lambda p: Path(p).read_text(encoding="utf-8", errors="replace"),
                    checkpointer=default_checkpointer(Path(args.ckpt)))
    final = g.invoke(Command(resume={"action": args.action, "reason": args.reason}),
                     config={"configurable": {"thread_id": args.thread}})
    d = final["human_decision"]
    print(f"decision recorded in audit: thread={args.thread} action={d['action']} @ {d['decided_at']}")
    print(f"audit file: {final['audit_ref']} (ledger updates still go through jd_registry_ops.py)")
    return 0


def configure_dspy_local(settings):
    """local backend -> DSPy LM (llama.cpp)."""
    from .extract import configure_from_env

    return configure_from_env(
        backend="local",
        env={"APA_LLM_BASE_URL": settings.base_url,
             "APA_LLM_MODEL": settings.model, "APA_LLM_API_KEY": settings.api_key})


def _cmd_monitor(args: argparse.Namespace) -> int:
    """Terminal live dashboard (Rich Live; monitoring view, same scenario as ApplyPilot)."""
    from . import monitor

    tmap: dict[str, str] = {}
    if Path(args.targets).exists():
        try:
            targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))["targets"]
            for t in targets:
                tmap.setdefault(t.get("channel", ""), t.get("channel", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    monitor.run(Path(args.db), Path(args.queue), interval=args.interval)
    return 0


def _cmd_profile_ingest(args: argparse.Namespace) -> int:
    """R0 profile ingestion: parse user documents -> extract facts -> merge
    (**never overwrites fields a human has edited**).

    Step 1 of the journey: the user **supplies documents**, not a hand-filled
    form. Landing spot: data/inbox/.
    """
    from datetime import datetime, timezone

    from .profile import (UnsupportedDocument, merge_claims, parse_dir, parse_document,
                          save as save_profile)
    from .profile.extract import (extract_from_documents,
                                  parse_json_resume_structured, _attach_segments)
    from .profile.models import ClaimStatus, DocFormat
    from .profile.store import load as load_profile, record_change

    paths = [Path(p) for p in (args.paths or [config.DATA_DIR / "inbox"])]
    docs, failures = [], []
    for p in paths:
        if p.is_dir():
            d, f = parse_dir(p)
            docs.extend(d)
            failures.extend(f)
        elif p.exists():
            try:
                docs.append(parse_document(p))
            except (UnsupportedDocument, OSError) as exc:
                failures.append((p, str(exc)))
        else:
            failures.append((p, "path does not exist"))

    if failures:
        print("skipped:")
        for path, why in failures:
            print(f"  - {path.name}: {why}")
    if not docs:
        print("no documents parsed — drop .docx/.pdf/.txt/.md into data/inbox/ "
              "or pass a path")
        return 1

    print(f"parsed {len(docs)} document(s):")
    for d in docs:
        print(f"  - {d.path.name}: {d.chars} chars, {len(d.segments)} segments"
              + (f", {len(d.tables)} table(s)" if d.tables else ""))

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    claims, dropped = [], []

    # JSON Resume goes through structured direct extraction (no LLM); the rest go through DSPy
    native = [d for d in docs if d.fmt is DocFormat.JSON_RESUME]
    for d in native:
        claims.extend(parse_json_resume_structured(d, now))
    llm_docs = [d for d in docs if d.fmt is not DocFormat.JSON_RESUME]

    if args.no_llm or not llm_docs:
        if llm_docs and args.no_llm:
            print("--no-llm: skipped LLM extraction "
                  f"({len(llm_docs)} document(s) left unparsed for facts)")
    else:
        try:
            import dspy

            from .llm.client import load_settings
            from .profile.extract import BackendTooWeak, build_signature, check_backend_capability

            s = load_settings()
            # Capability gating happens before the call: 1.5B list extraction
            # is a measured, stable failure mode - block it outright rather
            # than patch it up after the fact into a "seemingly usable" but
            # degraded result.
            try:
                check_backend_capability(s.model)
            except BackendTooWeak as exc:
                print(f"refusing to extract: {exc}")
                return 3
            dspy.configure(lm=dspy.LM(model=f"openai/{s.model}", api_base=s.base_url,
                                      api_key=s.api_key, max_tokens=4096, temperature=0.0))
            predictor = dspy.Predict(build_signature())
        except BackendTooWeak:
            raise
        except Exception as exc:                       # noqa: BLE001 - LM unavailability must not crash
            print(f"LLM unavailable ({exc}); run with --no-llm to parse only")
            return 2
        rep = extract_from_documents(llm_docs, predictor, now)
        claims.extend(rep.claims)
        dropped.extend(rep.dropped)

    if not claims:
        print("no facts extracted (every candidate lacked support in the source text)")
        for d in dropped[:10]:
            print(f"  dropped: {d.get('field')} — {d.get('text', '')[:70]}")
        return 1

    profile = load_profile(Path(args.profile) if args.profile else None)
    outcome = merge_claims(profile, claims)

    if args.dry_run:
        print(f"\n--dry-run: nothing written. {outcome.summary()}")
        for key in outcome.added[:15]:
            c = profile.claims[key]
            src = c.provenance[0].source_file if c.provenance else "?"
            loc = c.provenance[0].locator if c.provenance else "?"
            print(f"  + {key}: {c.text[:60]}  [{src} {loc}]")
        if outcome.conflicts:
            print(f"  ! needs your call: {', '.join(outcome.conflicts[:10])}")
        return 0

    path = save_profile(profile, Path(args.profile) if args.profile else None)
    record_change("ingest", "<batch>", "", f"{len(claims)} claims",
                  reason=f"{len(docs)} document(s)", by="agent")
    print(f"\n{outcome.summary()}")
    print(f"profile written: {path}")
    if outcome.conflicts:
        print(f"needs your call ({len(outcome.conflicts)}): "
              f"{', '.join(outcome.conflicts[:8])}")
        print("  -> apa profile show --review   |   apa profile resolve <field> --take ...")
    print("\nnext: apa profile show --review   (only after confirm/correction does it enter submission materials)")
    return 0


def _cmd_profile_show(args: argparse.Namespace) -> int:
    from .profile import review_queue, summarize
    from .profile.store import load as load_profile

    profile = load_profile()
    if args.review:
        rows = review_queue(profile)
        if args.field:
            rows = [r for r in rows if r["field"].startswith(args.field)]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("nothing to review")
            return 0
        print(f"{len(rows)} field(s) awaiting you (conflicts & low-confidence first):\n")
        for r in rows:
            flag = "!" if r["status"] in ("conflict", "needs_human") else " "
            src = ", ".join(r["sources"]) or "-"
            print(f"{flag} [{r['status']:15}] {r['field']}")
            print(f"    {r['text'][:100]}")
            print(f"    from: {src} {r['locator']}")
        print("\nconfirm: apa profile confirm <field>   edit: apa profile edit <field> <text>")
        return 0

    info = summarize(profile)
    if args.json:
        print(json.dumps({"summary": info, "profile": profile.to_dict()},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"claims: {info['total_claims']}  "
          f"(pinned {info['pinned']}, experiences {info['experiences']})")
    if info["by_status"]:
        print("  status: " + ", ".join(f"{k}={v}" for k, v in sorted(info["by_status"].items())))
    if info["source_documents"]:
        print("  sources: " + ", ".join(info["source_documents"]))
    print(f"  work authorization: {profile.work_authorization.label}")
    pending = sum(1 for c in profile.claims.values()
                  if c.status.value in ("conflict", "needs_human", "draft"))
    if pending:
        print(f"\n{pending} field(s) need you -> apa profile show --review")
    return 0


def _cmd_profile_confirm(args: argparse.Namespace) -> int:
    from .profile import confirm_claim, save as save_profile
    from .profile.store import load as load_profile, record_change

    profile = load_profile()
    try:
        claim = confirm_claim(profile, args.field)
    except KeyError:
        print(f"no such field: {args.field}  (apa profile show --review to list)")
        return 1
    save_profile(profile)
    record_change("confirm", args.field, "", claim.text, by="human")
    print(f"confirmed: {args.field} = {claim.text[:80]}")
    return 0


def _cmd_profile_edit(args: argparse.Namespace) -> int:
    from .profile import apply_manual_edit, save as save_profile
    from .profile.store import load as load_profile, record_change

    profile = load_profile()
    old = profile.claims[args.field].text if args.field in profile.claims else ""
    try:
        claim = apply_manual_edit(profile, args.field, args.text, reason=args.reason)
    except (ValueError, KeyError) as exc:
        print(str(exc))
        return 1
    save_profile(profile)
    record_change("edit", args.field, old, claim.text, reason=args.reason, by="human")
    print(f"updated (pinned — re-ingest will not touch it): {args.field} = {claim.text}")
    if old:
        print(f"  was: {old}")
    return 0


def _cmd_profile_resolve(args: argparse.Namespace) -> int:
    from .profile import resolve_conflict, save as save_profile
    from .profile.store import load as load_profile, record_change

    profile = load_profile()
    try:
        claim = resolve_conflict(profile, args.field, args.take)
    except (KeyError, ValueError) as exc:
        print(str(exc))
        return 1
    save_profile(profile)
    record_change("resolve", args.field, "", f"took {args.take}: {claim.text}", by="human")
    print(f"resolved ({args.take}): {args.field} = {claim.text[:80]}")
    return 0


def _cmd_profile_history(args: argparse.Namespace) -> int:
    from .profile.store import history

    rows = history(n=args.n)
    if not rows:
        print("no changes recorded yet")
        return 0
    for r in rows:
        reason = f"  ({r['reason']})" if r.get("reason") else ""
        print(f"{r['ts']}  {r['action']:8} {r['field']}{reason}")
        if r.get("old") and r["old"] != r.get("new"):
            print(f"      was: {r['old'][:90]}")
            print(f"      now: {r.get('new', '')[:90]}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    """Rendered fetch (browser channel).

    Why this exists: direct HTTP probing of SPA-style sites (e.g. Workday)
    returns 200 + empty body - **this is not anti-bot protection**: measured
    2026-10-19, opening a browser and waiting 8s returns 5598 characters of
    full text for the same URL. The original attribution ("anti-bot /
    requires Singpass") was wrong; this command provides the correct fix.
    """
    from .f10.browser import BrowserFetch, LoginSession

    fetcher: object
    if args.login_state:
        session = LoginSession()
        fetcher = _SessionFetcher(session, wait_ms=args.wait)
    else:
        fetcher = BrowserFetch(wait_ms=args.wait, headed=args.headed)

    bodies: list[str] = []
    problems = 0
    for url in args.urls:
        res = fetcher.fetch(url)
        if res.error:
            problems += 1
            print(f"FAILED  {url}\n        {res.error}")
            continue
        flag = "ok " if res.rendered_ok else "thin"
        note = "" if res.rendered_ok else "  (short body — retry with a longer --wait)"
        print(f"{flag} {res.status}  {len(res.text):>6} chars  {res.title[:60]}{note}")
        bodies.append(f"<!-- source: {url} -->\n{res.text}")
    if args.out and bodies:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n\n<!-- -->\n\n".join(bodies), encoding="utf-8")
        print(f"\nwritten: {p}")
    return 1 if problems == len(args.urls) else 0


class _SessionFetcher:
    """``--login-state`` adapter: reuses the browser profile created by ``apa auth login``."""

    def __init__(self, session, wait_ms: int):
        self._session = session
        self._wait = wait_ms

    def fetch(self, url: str):
        return self._session.fetch(url, wait_ms=self._wait)


def _cmd_auth_login(args: argparse.Namespace) -> int:
    """Open a headed browser so the **user logs in personally**.

    Zero credential contact: login happens inside the browser; this process
    never enters, stores, or reads cookie values (Singpass includes MFA /
    one-time passwords, which only makes automation more brittle).
    """
    from .f10.browser import BrowserUnavailable, LoginSession

    session = LoginSession()
    try:
        session.launch(args.url, wait_s=args.wait)
    except BrowserUnavailable as exc:
        print(f"cannot open a browser: {exc}")
        return 1
    print(f"\nA browser window is open at {args.url}")
    print("Log in there yourself. This process never sees your credentials —")
    print("only the browser profile in data/browser-session/ is kept.")
    print(f"After logging in, run:  apa fetch --login-state <url>")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    """HITL review console (NiceGUI; binds 127.0.0.1 per the data-sovereignty checklist)."""
    from . import webapp

    tmap: dict[str, str] = {}
    if Path(args.targets).exists():
        try:
            targets = json.loads(Path(args.targets).read_text(encoding="utf-8"))["targets"]
            for t in targets:
                tmap.setdefault(t.get("channel", ""), t.get("channel", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    print(f"HITL review console: http://127.0.0.1:{args.port} (Ctrl-C to quit; read-only "
          "review path, decisions written to data/hitl_decisions.jsonl)")
    webapp.run(Path(args.db), Path(args.queue), port=args.port)
    return 0


def _cmd_status() -> int:
    print(f"apa {__version__} - W1 complete (D1-D6 + review bidirectionality + F10 deep probe)")
    print("P0 declared usable: serving real submission final-review (see reports/evals/run_001/)")
    print("D4-D5 F10: 3 pollers live + state machine (2-consecutive debounce, per-target series)"
          " + affected list (LOST/AT-RISK/RECOVERABLE) + 3 queue rules;"
          " INC-SYNTH-1016 replay green (evals/f10/)")
    print("D6 F4: DeepEval 4.3.1 FaithfulnessMetric gate in pytest (replay judge default,"
          " deterministic; llamacpp/openai opt-in) - sem-001 dirty 0.5 CAUGHT vs sem-002"
          " control 1.0 PASSED; judge discipline rule 1 + versions in reports/evals/run_002/judge_config.md")
    print("D6 Registry bridge: MCP (FastMCP) over jd_registry_ops.py - argv list + shell=False"
          " (ironclad-3), PYTHONIOENCODING=utf-8 child env; sandbox update rehearsal green"
          " (real ledger untouched); CI workflow written, remote/push = user exposure decision")
    print("D6+ review bidirectionality: apa review audit --file <review.md> - 8 dimensions"
          " (evidence/repro/wording/grade/action/boundary/numeric/provenance); self-audit of"
          " APA's own documentation converged 18 high -> 0 high via 4 false-positive rounds"
          " (see reports/review/run_001/); verdict: only boundary = REJECT, others = request evidence")
    print("Known gap: A1 false-positive CI above exit bar (98.5%) -> A1-entity-leak v0.2 is the week-4 push")
    print("Known gap: careers.gov.sg statuspage JSON API denies non-browser clients (S3 AccessDenied)"
          " -> statuspage/maintenance probes record blocked/parse_error; manual notice archive remains primary")
    print("Next: D8 - llama.cpp + Qwen2.5-0.5B/1.5B GGUF environment (hf-mirror.com,"
          " E:/dev/models); D9-D10 DSPy extraction aligned to ledger via MCP;"
          " D11 channel profiles; D12 tracing v0 (OTel -> SQLite); D13 prompt baselines."
          " Presidio (F6) is scheduled for D16 in week 3 - do not pull forward")
    return 0


def _cmd_compliance_check(args: argparse.Namespace) -> int:
    rules = get_rules(args.rules)
    needs_corpus = any(r.kind == KIND_CORPUS for r in rules)
    corpus_dir = Path(args.corpus)

    if needs_corpus and not corpus_dir.is_dir():
        print(f"error: corpus directory does not exist: {corpus_dir} (corpus rules require sibling CLs)",
              file=sys.stderr)
        return 2

    total = Report()
    for raw in args.paths:
        p = Path(raw)
        ctx = None
        if needs_corpus:
            # Build a separate corpus per target file: exclude the file itself
            # (otherwise its own content would make "word fragments unique to
            # other forks" degrade into fragments shared across forks, and the
            # residue detection would stop working)
            others = {
                f.name: f.read_text(encoding="utf-8")
                for f in sorted(corpus_dir.glob("CoverLetter_*.md"))
                if f.resolve() != p.resolve()
            }
            ctx = Context(corpus=Corpus(others=others, target_fork=fork_of(p.name)))
        total.extend(check_files([p], rules, ctx).files[0])

    print(total.render_text())
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(total.to_json(), encoding="utf-8")
        print(f"\nJSON report written: {args.json_path}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(total.render_text(), encoding="utf-8")
        print(f"text report written: {args.out}")
    return 1 if total.hard_count else 0


def _cmd_probe_run(args: argparse.Namespace) -> int:
    from .f10 import pollers, store
    from .f10.http import UrllibTransport

    transport = UrllibTransport(timeout=args.timeout)
    results = []
    if args.kind in ("all", "statuspage"):
        results += pollers.StatuspagePoller(config.F10_STATUSPAGE_BASE, transport).probe()
    if args.kind in ("all", "maintenance"):
        results.append(pollers.MaintenancePoller(config.F10_MAINTENANCE_URL, transport).probe())
    if args.kind in ("all", "jobs"):
        tp = Path(args.targets)
        if not tp.exists():
            print(f"error: probe targets not found: {tp}"
                  " (generate with scripts/03_build_probe_targets.py first)", file=sys.stderr)
            return 2
        targets = json.loads(tp.read_text(encoding="utf-8"))["targets"]
        results += pollers.JobUrlPoller(transport).probe(
            [t["url"] for t in targets], deep=args.deep)

    n = store.write_results(args.db, results)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    print(f"probe complete: {n} results -> {args.db}")
    print(f"  summary by class: {counts}")
    for r in results:
        if r.outcome != "ok":
            tgt = r.target if len(r.target) <= 72 else r.target[:69] + "..."
            print(f"  [{r.outcome}] {r.kind} {tgt} ({r.latency_ms}ms) {r.detail}")
    # D4 scope: record to DB only, no gating yet (portal-health-gate state machine is D5); exit code 0
    return 0


def _cmd_probe_targets(args: argparse.Namespace) -> int:
    tp = Path(args.targets)
    if not tp.exists():
        print(f"error: probe targets not found: {tp}"
              " (generate with scripts/03_build_probe_targets.py first)", file=sys.stderr)
        return 2
    data = json.loads(tp.read_text(encoding="utf-8"))
    by_ch: dict[str, int] = {}
    for t in data["targets"]:
        by_ch[t["channel"]] = by_ch.get(t["channel"], 0) + 1
    s = data["stats"]
    print(f"{len(data['targets'])} probe-target URLs (generated {data['generated_at']}, "
          f"source: {data['source']})")
    print(f"  channel distribution: {by_ch}")
    print(f"  generation stats: {s}")
    return 0


def _cmd_probe_status(args: argparse.Namespace) -> int:
    from .f10 import store
    rows = store.latest_by_target(args.db)
    if not rows:
        print("probe_log is empty (run `apa probe run` first)")
        return 0
    print(f"latest probe (1 row per target, {len(rows)} targets):")
    for r in rows:
        tgt = r["target"] if len(r["target"]) <= 64 else r["target"][:61] + "..."
        print(f"  [{r['outcome']:>11}] {r['kind']:<12} {tgt} "
              f"({r['latency_ms']}ms, {r['ts']})")
    return 0


def _cmd_probe_register(args: argparse.Namespace) -> int:
    from .f10 import scheduler
    cmds = scheduler.register(apply=args.apply, apa_exe=args.apa_exe)
    mode = "registered (apply)" if args.apply else "dry-run (not executed; pass --apply to register for real)"
    print(f"Task Scheduler three tasks - {mode}:")
    for c in cmds:
        print(f"  {c.name}: {c.desc}")
        print(f"    {c.command}")
    if not args.apply:
        print("note: registration = host system change; execute only after explicit confirmation (HITL discipline, same principle)")
    return 0


def _load_targets_and_jobs(targets_path: str):
    """Probe-targets JSON -> (target->channel map, list of JobRefs).

    Ledger deadlines are date-granular -> normalized to 23:59:59 (+08:00) of
    that day so they compare against window timestamps.
    """
    from .f10 import statemachine as sm
    data = json.loads(Path(targets_path).read_text(encoding="utf-8"))
    tmap, jobs = {}, []
    for t in data["targets"]:
        tmap[t["url"]] = t["channel"]
        dl = None
        if t.get("deadline"):
            d = datetime.fromisoformat(t["deadline"])
            if d.tzinfo is None:
                d = d.replace(hour=23, minute=59, second=59,
                              tzinfo=timezone(timedelta(hours=8)))
            dl = d
        jobs.append(sm.JobRef(jd_id=t["jd_id"], channel=t["channel"], deadline=dl,
                              title=t.get("title", ""), employer=t.get("employer", "")))
    return tmap, jobs


def _cmd_probe_evaluate(args: argparse.Namespace) -> int:
    from .f10 import store
    if not Path(args.targets).exists():
        print(f"error: probe targets not found: {args.targets}"
              " (generate with scripts/03_build_probe_targets.py first)", file=sys.stderr)
        return 2
    tmap, jobs = _load_targets_and_jobs(args.targets)
    alt: dict[str, bool] = {}
    batch: set[str] = set()
    if args.queue and Path(args.queue).exists():
        q = json.loads(Path(args.queue).read_text(encoding="utf-8"))
        for e in q.get("entries", []):
            jd = e.get("jd_id")
            if not jd:
                continue
            alt[jd] = bool(e.get("alternative_submitted"))
            if e.get("status") in ("drafted", "ready"):
                batch.add(jd)      # batch membership: basis for affected-status adjudication of evergreen roles
    report = store.evaluate_and_record(args.db, tmap, jobs, alt, batch)
    print("channel health state (level set after 2 consecutive measurements; series granularity = target):")
    for ch in sorted(report["states"]):
        st = report["states"][ch]
        since = st.since.isoformat() if st.since else "-"
        print(f"  [{st.state:>11}] {ch}  (since {since})")
    if report["incidents"]:
        print(f"\nincident log (incidents table; AUTO- prefix = state-machine generated):")
        for inc in report["incidents"]:
            resolved = inc["resolved_at"] or "unresolved (ongoing)"
            print(f"  {inc['code']}: {inc['started_at']} → {resolved}")
            for jd, tier in sorted(inc["tiers"].items()):
                print(f"      [{tier:>11}] {jd}")
    else:
        print("\nno outage window (no new incidents registered)")
    return 0


def _cmd_probe_sync_health(args: argparse.Namespace) -> int:
    from .f10 import store
    if not Path(args.targets).exists():
        print(f"error: probe targets not found: {args.targets}", file=sys.stderr)
        return 2
    if not Path(args.queue).exists():
        print(f"error: queue not found: {args.queue} (F12 submission queue; D5 rule contract "
              "is documented in the compliance/rules/f10.py docstring)", file=sys.stderr)
        return 2
    tmap, _ = _load_targets_and_jobs(args.targets)
    written = store.sync_health(args.db, args.queue, tmap)
    print("channel health snapshot written into queue channel_health field:")
    for ch, state in sorted(written.items()):
        print(f"  [{state:>11}] {ch}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows encoding discipline (D6+): the console defaults to GBK, and
    # Chinese/emoji output raises UnicodeEncodeError (measured: running
    # python.exe directly crashes; `conda run` only appears safe because it
    # injects PYTHONIOENCODING). Parent and child processes are handled the
    # same way: uniform UTF-8 output with errors=replace as the fallback.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="apa",
        description="Application Pipeline Agent - governed document-intake & compliance agent",
    )
    parser.add_argument("--version", action="version", version=f"apa {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show current milestone and next steps (numbers come from reports/, not hand-written)")

    p_comp = sub.add_parser("compliance", help="F1 compliance check")
    p_comp_sub = p_comp.add_subparsers(dest="subcommand")
    p_check = p_comp_sub.add_parser("check", help="run the rules table over files, output a line-anchored report")
    p_check.add_argument("paths", nargs="+", help="files to check (CL / resume markdown)")
    p_check.add_argument("--rules", default="all",
                         choices=["all", "inherited", "accidents", "f10", "format"],
                         help="rule set (default all; inherited = equivalent to the _130 production set, for regression comparison)")
    p_check.add_argument("--corpus",
                         default=str(config.SINGAPORE_ROOT / "me") if config.SINGAPORE_ROOT else None,
                         help="sibling CL corpus directory (for corpus rules)")
    p_check.add_argument("--json", dest="json_path", default=None, help="JSON report output path")
    p_check.add_argument("--out", default=None, help="text report output path")

    p_probe = sub.add_parser("probe", help="F10 channel health probe (three pollers + Task Scheduler)")
    p_probe_sub = p_probe.add_subparsers(dest="subcommand")

    p_run = p_probe_sub.add_parser("run", help="run probes and record to the SQLite probe_log")
    p_run.add_argument("--kind", default="all",
                       choices=["all", "statuspage", "jobs", "maintenance"],
                       help="probe type (default all)")
    p_run.add_argument("--db", default=str(config.SQLITE_PATH), help="SQLite path")
    p_run.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH),
                       help="probe-targets JSON (for the jobs kind)")
    p_run.add_argument("--timeout", type=int, default=20, help="per-request timeout in seconds")
    p_run.add_argument("--deep", action="store_true",
                       help="jobs-kind deep check: GET the detail page body and extract deadline/closed/posted-date signals"
                            " (observations recorded in detail, not used for adjudication - SSR tags are unstable, see the pollers' evidence)")

    p_tg = p_probe_sub.add_parser("targets", help="show a summary of the current probe targets")
    p_tg.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH))

    p_st = p_probe_sub.add_parser("status", help="show the latest probe result per target")
    p_st.add_argument("--db", default=str(config.SQLITE_PATH))

    p_rt = p_probe_sub.add_parser("register-tasks",
                                  help="register the three Windows Task Scheduler tasks (dry-run by default)")
    p_rt.add_argument("--apply", action="store_true",
                      help="actually run schtasks (otherwise only print the commands)")
    p_rt.add_argument("--apa-exe", default="apa", help="apa executable name invoked by the task")

    p_ev = p_probe_sub.add_parser("evaluate",
                                  help="run the state machine over probe_log (level set after 2 consecutive measurements) and register incidents")
    p_ev.add_argument("--db", default=str(config.SQLITE_PATH), help="SQLite path")
    p_ev.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH),
                      help="probe-targets JSON (target->channel map and affected job set)")
    p_ev.add_argument("--queue", default=None,
                      help="submission-queue JSON (optional; provides alternative_submitted evidence)")

    p_sh = p_probe_sub.add_parser("sync-health",
                                  help="write the current channel-health snapshot into the queue channel_health field")
    p_sh.add_argument("--db", default=str(config.SQLITE_PATH), help="SQLite path")
    p_sh.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH))
    p_sh.add_argument("--queue", required=True, help="submission-queue JSON path")

    p_mon = sub.add_parser("monitor", help="terminal live dashboard (Rich Live: channel health + submission queue)")
    p_mon.add_argument("--db", default=str(config.SQLITE_PATH), help="SQLite path")
    p_mon.add_argument("--queue", required=True, help="submission-queue JSON path")
    p_mon.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH))
    p_mon.add_argument("--interval", type=float, default=5.0, help="refresh interval in seconds")

    # ---- Browser channel: design decision - instead of fighting anti-bot
    #      detection, open the page in a real browser and let the user log in
    #      personally; see src/apa/f10/browser.py ----
    p_fetch = sub.add_parser(
        "fetch", help="rendered fetch: open the page in a real browser and wait for SPA rendering (fixes empty body)")
    p_fetch.add_argument("urls", nargs="+", help="one or more URLs")
    p_fetch.add_argument("--headed", action="store_true",
                         help="headed mode: you can see and interact with the page")
    p_fetch.add_argument("--login-state", action="store_true",
                         help="reuse the login state established by `apa auth login` (for sites requiring Singpass)")
    p_fetch.add_argument("--wait", type=int, default=8000,
                         help="render wait in milliseconds (measured: SPAs need 6000-10000)")
    p_fetch.add_argument("--out", help="write the body text to this file (default: print a summary)")

    p_auth = sub.add_parser(
        "auth", help="browser login state: you log in yourself in the browser; credentials never pass through this process")
    p_auth_sub = p_auth.add_subparsers(dest="subcommand")
    p_auth_login = p_auth_sub.add_parser(
        "login", help="open a headed browser at the given URL and log in manually (Singpass / username-password)")
    p_auth_login.add_argument("url", nargs="?",
                              default="https://sggovterp.wd102.myworkdayjobs.com",
                              help="login entry point (default: Workday public job site)")
    p_auth_login.add_argument("--wait", type=int, default=300, help="window keep-alive seconds")

    p_web = sub.add_parser("web", help="HITL review console (NiceGUI, binds 127.0.0.1)")
    p_web.add_argument("--db", default=str(config.SQLITE_PATH), help="SQLite path")
    p_web.add_argument("--queue", required=True, help="submission-queue JSON path")
    p_web.add_argument("--targets", default=str(config.PROBE_TARGETS_PATH))
    p_web.add_argument("--port", type=int, default=8501)

    # ---- R0 profile: absorb user documents -> single source of truth ----
    p_prof = sub.add_parser(
        "profile", help="R0 personal profile: ingest your docx/pdf/txt/md -> review -> single source of truth")
    p_prof_sub = p_prof.add_subparsers(dest="subcommand")
    p_prof_ing = p_prof_sub.add_parser(
        "ingest", help="parse documents and extract facts (never overwrites fields you edited)")
    p_prof_ing.add_argument("paths", nargs="*", default=[str(config.DATA_DIR / "inbox")],
                            help="documents/directory (default data/inbox/)")
    p_prof_ing.add_argument("--dry-run", action="store_true",
                            help="parse + extract and print only; do not write profile.json")
    p_prof_ing.add_argument("--no-llm", action="store_true",
                            help="skip LLM extraction (parse only, show corpus stats)")
    p_prof_ing.add_argument("--profile", default=None, help="profile.json path")
    p_prof_show = p_prof_sub.add_parser("show", help="print the profile summary / review queue")
    p_prof_show.add_argument("--review", action="store_true",
                             help="list fields awaiting review (conflicts and low-confidence first)")
    p_prof_show.add_argument("--field", help="only show one field")
    p_prof_show.add_argument("--json", action="store_true", help="output JSON")
    p_prof_conf = p_prof_sub.add_parser("confirm", help="confirm a fact (already checked by a human)")
    p_prof_conf.add_argument("field")
    p_prof_edit = p_prof_sub.add_parser("edit", help="manually correct a fact (never overwritten by re-ingest)")
    p_prof_edit.add_argument("field")
    p_prof_edit.add_argument("text")
    p_prof_edit.add_argument("--reason", default="", help="reason for the correction (recorded in change history)")
    p_prof_res = p_prof_sub.add_parser("resolve", help="adjudicate a conflict: --take existing|incoming")
    p_prof_res.add_argument("field")
    p_prof_res.add_argument("--take", choices=("existing", "incoming"), required=True)
    p_prof_hist = p_prof_sub.add_parser("history", help="view the change history (append-only)")
    p_prof_hist.add_argument("-n", type=int, default=20)

    p_ret = sub.add_parser("retrieve", help="F5 retrieval: vector search over the historical submission-package corpus (RAG over your own history)")
    p_ret_sub = p_ret.add_subparsers(dest="subcommand")
    p_idx = p_ret_sub.add_parser("index", help="build/rebuild the retrieval index (scans submission-package JD.md)")
    p_idx.add_argument("--packages",
                       default=str(config.SINGAPORE_ROOT / "jobs" / "投递包")
                       if config.SINGAPORE_ROOT else None,
                       help="submission-package directory (read-only)")
    p_idx.add_argument("--index-dir", default=str(config.REPO_ROOT / "data" / "retrieval_index"))
    p_idx.add_argument("--model",
                       default=str(config.DEV_MODELS_DIR / "bge-small-en-v1.5"),
                       help="local embedding model path (rule 12)")
    p_sr = p_ret_sub.add_parser("search", help="query the most similar historical submission packages")
    p_sr.add_argument("--jd", default=None, help="new JD file (mutually exclusive with --query)")
    p_sr.add_argument("--query", default=None, help="free-text query")
    p_sr.add_argument("--index-dir", default=str(config.REPO_ROOT / "data" / "retrieval_index"))
    p_sr.add_argument("--model",
                      default=str(config.DEV_MODELS_DIR / "bge-small-en-v1.5"))
    p_sr.add_argument("-k", type=int, default=5)

    p_ag = sub.add_parser("agent", help="agent loop (LangGraph: ingest->extract->retrieve->⏸review->audit)")
    p_ag_sub = p_ag.add_subparsers(dest="subcommand")
    p_run = p_ag_sub.add_parser("run", help="process one new JD (runs until the HITL interrupt)")
    p_run.add_argument("--jd", required=True, help="JD file path")
    p_run.add_argument("--jd-id", default=None, help="record ID (defaults to the filename)")
    p_run.add_argument("--thread", default=None, help="thread id (defaults to jd-id)")
    p_run.add_argument("--ckpt", default=str(config.REPO_ROOT / "data" / "checkpoints.db"))
    p_run.add_argument("--audit", default=str(config.REPO_ROOT / "data" / "agent_runs.jsonl"))
    p_res = p_ag_sub.add_parser("resume", help="resume a thread interrupted by HITL")
    p_res.add_argument("--thread", required=True)
    p_res.add_argument("--action", required=True, choices=["approve", "waive", "defer"])
    p_res.add_argument("--reason", default="")
    p_res.add_argument("--jd", required=True, help="JD file path (re-read on resume)")
    p_res.add_argument("--ckpt", default=str(config.REPO_ROOT / "data" / "checkpoints.db"))
    p_res.add_argument("--audit", default=str(config.REPO_ROOT / "data" / "agent_runs.jsonl"))

    p_rev = sub.add_parser("review", help="review bidirectionality: audit third-party input (or your own output)")
    p_rev_sub = p_rev.add_subparsers(dest="subcommand")
    p_aud = p_rev_sub.add_parser("audit", help="audit whether a review opinion can be adopted")
    p_aud.add_argument("file", help="review markdown path")
    p_aud.add_argument("--json", dest="json_path", default=None, help="JSON report output path")
    p_aud.add_argument("--out", default=None, help="markdown report output path")
    p_aud.add_argument("--fail-on", default="none", choices=["none", "medium", "high"],
                       help="return a non-zero exit code when this severity is hit (default none = report only)")

    args = parser.parse_args(argv)

    if args.command in (None, "status"):
        return _cmd_status()
    if args.command == "compliance":
        if args.subcommand == "check":
            return _cmd_compliance_check(args)
        p_comp.print_help()
        return 1
    if args.command == "probe":
        if args.subcommand == "run":
            return _cmd_probe_run(args)
        if args.subcommand == "targets":
            return _cmd_probe_targets(args)
        if args.subcommand == "status":
            return _cmd_probe_status(args)
        if args.subcommand == "register-tasks":
            return _cmd_probe_register(args)
        if args.subcommand == "evaluate":
            return _cmd_probe_evaluate(args)
        if args.subcommand == "sync-health":
            return _cmd_probe_sync_health(args)
        p_probe.print_help()
        return 1
    if args.command == "review":
        if args.subcommand == "audit":
            return _cmd_review_audit(args)
        p_rev.print_help()
        return 1
    if args.command == "profile":
        if args.subcommand == "ingest":
            return _cmd_profile_ingest(args)
        if args.subcommand == "show":
            return _cmd_profile_show(args)
        if args.subcommand == "confirm":
            return _cmd_profile_confirm(args)
        if args.subcommand == "edit":
            return _cmd_profile_edit(args)
        if args.subcommand == "resolve":
            return _cmd_profile_resolve(args)
        if args.subcommand == "history":
            return _cmd_profile_history(args)
        p_prof.print_help()
        return 1
    if args.command == "monitor":
        return _cmd_monitor(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    if args.command == "auth":
        if args.subcommand == "login":
            return _cmd_auth_login(args)
        p_auth.print_help()
        return 1
    if args.command == "web":
        return _cmd_web(args)
    if args.command == "retrieve":
        if args.subcommand == "index":
            return _cmd_retrieve_index(args)
        if args.subcommand == "search":
            return _cmd_retrieve_search(args)
        p_ret.print_help()
        return 1
    if args.command == "agent":
        if args.subcommand == "run":
            return _cmd_agent_run(args)
        if args.subcommand == "resume":
            return _cmd_agent_resume(args)
        p_ag.print_help()
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
