"""APA web console: main UI (NiceGUI, binds 127.0.0.1).

Split screens by information architecture (user-journey order is navigation order):

    /             Today    what needs me right now (only 0-3 items that truly need a human call)
    /queue        Queue    which to apply for (deadline-sorted · three columns · right column has exactly three actions)
    /profile      Profile  are my facts right (**grouped by experience**, not a flat field list)
    /profile/all  all facts (power-user mode, per-item editing allowed)

    Audit and system settings are terminal-only (``apa monitor`` / config files), not web
    screens (docs/22 §1: three screens, not six).

Design discipline:
- bind 127.0.0.1 (never 0.0.0.0); ``DO_NOT_TRACK=1`` disables telemetry; no external CDN;
- the top-bar data-sovereignty statement **follows the actual backend**: in cloud mode it
  never claims "data stays on this machine" - that would be a kind lie;
- the UI implements no business logic; everything calls ``apa.profile`` (thin-pointer discipline);
- write actions append to disk immediately (a UI crash loses no decisions);
- ledger updates still go through the jd_registry_ops.py single channel; the UI is only a decision-collection endpoint;
- **no bulk confirm** (a select-all box is a loaded gun) - deciding item by item is part of the process.

Usage: ``apa web --queue <path> --port 8501``
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

HITL_LOG = Path("data") / "hitl_decisions.jsonl"


# ---- data assembly (pure functions, independently testable - the UI is only a presentation layer) ----

def load_channel_states(db_path: Path, target_channel: dict[str, str]) -> list[dict[str, Any]]:
    """Channel health snapshot (UI read-only path)."""
    from .f10 import store

    if not db_path.exists():
        return []
    states = store.evaluate(db_path, target_channel)
    return [
        {"channel": ch, "state": st.state,
         "since": st.since.strftime("%Y-%m-%d %H:%M") if st.since else "-"}
        for ch, st in sorted(states.items())
    ]


def load_queue(queue_path: Path) -> list[dict[str, Any]]:
    """Submission queue, deadline-sorted (no deadline sinks to the bottom)."""
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = data.get("entries", [])
    return sorted(entries, key=lambda e: str(e.get("deadline") or "9999-99-99"))


def record_hitl_decision(jd_id: str, action: str, reason: str,
                         log_path: Path = HITL_LOG) -> dict[str, Any]:
    """Persist a HITL decision (append-only jsonl - audit discipline).

    action in {"approve", "waive", "defer"}; reason is required (mandatory for
    waive). This is the UI's **only write path** - it does not touch the ledger
    (ledger updates are executed by a human via the single ops channel).
    """
    if action not in ("approve", "waive", "defer"):
        raise ValueError(f"unknown HITL action: {action!r}")
    if action == "waive" and not reason.strip():
        raise ValueError("waive requires a reason (audit discipline)")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "decision_by": "human (web console)",
        "jd_id": jd_id,
        "action": action,
        "reason": reason.strip(),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_recent_decisions(log_path: Path = HITL_LOG, n: int = 10) -> list[dict[str, Any]]:
    """The n most recent HITL decisions (shown on the review page)."""
    if not log_path.exists():
        return []
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines[-n:] if l.strip()]


# ---- NiceGUI pages (deferred import: nicegui is only needed for `apa web`) ----


# ---- actions (CLI and UI share the same source: the UI implements no business logic, thin-pointer discipline) ----

def profile_actions() -> dict[str, Any]:
    """Profile read/write actions shared by CLI and UI."""
    from .profile import (apply_manual_edit, confirm_claim, mark_needs_human,
                          merge_claims, resolve_conflict)
    from .profile.extract import extract_from_documents, parse_json_resume_structured
    from .profile.models import DocFormat
    from .profile.store import load, record_change, review_queue, save, summarize

    def ingest_dir(inbox: Path) -> tuple[dict, list]:
        """Parse a folder, extract facts, merge into the profile.

        Capability gate runs **before** the LLM call (docs/20): list
        extraction on a 1.5B model is a measured capability boundary
        (reports/f3/run_001.md), so we refuse rather than return partial
        output that looks usable.
        """
        from datetime import datetime, timezone

        from .profile import parse_dir
        from .profile.extract import (BackendTooWeak, build_signature,
                                      check_backend_capability)

        docs, failures = parse_dir(inbox)
        if not docs:
            return {"extracted": 0}, failures
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        claims = [c for d in docs if d.fmt is DocFormat.JSON_RESUME
                  for c in parse_json_resume_structured(d, now)]
        llm_docs = [d for d in docs if d.fmt is not DocFormat.JSON_RESUME]
        if llm_docs:
            try:
                import dspy

                from .llm.client import load_settings

                s = load_settings()
                check_backend_capability(s.model)
                dspy.configure(lm=dspy.LM(model=f"openai/{s.model}",
                                          api_base=s.base_url, api_key=s.api_key,
                                          max_tokens=4096, temperature=0.0))
                rep = extract_from_documents(llm_docs,
                                             dspy.Predict(build_signature()), now)
                claims.extend(rep.claims)
            except BackendTooWeak as exc:
                failures.append((Path("[capability]"), str(exc)))
            except Exception as exc:                    # noqa: BLE001
                failures.append((Path("[llm]"), f"LLM unavailable: {exc}"))
        profile = load()
        outcome = merge_claims(profile, claims)
        save(profile)
        if outcome.touched:
            record_change("ingest", "<batch>", "", f"{len(claims)} claims",
                          reason=f"{len(docs)} doc(s)", by="ui")
        return {"extracted": len(claims), **outcome.to_dict()}, failures

    return {"load": load, "save": save, "review_queue": review_queue,
            "summarize": summarize, "ingest_dir": ingest_dir,
            "confirm": confirm_claim, "edit": apply_manual_edit,
            "resolve": resolve_conflict, "needs_human": mark_needs_human,
            "record": record_change}


# ---- top bar: three-state data-sovereignty badge ----

NAV = (("Today", "/"), ("Queue", "/queue"), ("Profile", "/profile"),
       ("Settings", "/settings"))

_db_path = config.SQLITE_PATH
_queue_path = config.DATA_DIR / "probe_queue.json"
_target_channel: dict[str, str] = {}


def _inbox_dir() -> Path:
    return config.DATA_DIR / "inbox"


def _short(text: str, n: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "..."


def _topbar(active: str) -> None:
    """Top bar. The sovereignty label follows the real backend.

    In cloud mode it never says "data stays on this machine" — that would
    be a kind lie. Outbound mode is clickable so you can see what left.
    """
    from nicegui import ui

    from .sovereignty import current_status

    st = current_status()
    with ui.row().classes("w-full items-center gap-3 px-4 py-2 no-wrap"):
        ui.label("APA").classes("text-h6")
        badge = ui.badge(st.label, color=st.color)
        if st.detailable:
            with ui.dialog() as dlg, ui.card():
                ui.label("What actually left this machine").classes("text-h6")
                for line in st.detail_lines():
                    ui.label(line).classes("text-sm font-mono")
                ui.label("PII is checked before anything is sent — findings block "
                         "the call instead of being silently redacted."
                         ).classes("text-xs text-gray-600")
                ui.button("close", on_click=dlg.close).props("flat")
            badge.on("click", dlg.open)
            badge.classes("cursor-pointer")
        ui.space()
        for label, href in NAV:
            cls = "text-sm " + ("text-weight-bold text-primary" if label == active
                                 else "text-grey")
            ui.link(label, href).classes(cls)
        ui.button("reload", on_click=lambda: ui.navigate.reload()).props("flat dense")


def _section(title: str, subtitle: str = "") -> None:
    """Screen heading states a status, never assigns a task."""
    from nicegui import ui

    ui.label(title).classes("text-h5 q-mb-none")
    if subtitle:
        ui.label(subtitle).classes("text-sm text-gray-600 q-mt-none q-mb-sm")


# ================= screen 1: Today (default landing) =================

def build_today(actions: dict[str, Any]) -> None:
    """What needs you right now — deadlines first, then replies, then facts.

    Never lists every claim: 25 cards would be 25 rubber stamps in
    disguise. With nothing to decide it still offers a next step.
    """
    from nicegui import ui

    from . import uiview
    from .profile.router import build_queue, health_metrics

    _topbar("Today")

    # This is a job-search console, not a profile admin page: what is
    # time-critical (a closing deadline, a reply that never came) comes
    # before fact arbitration. The old Today only showed the latter.
    prefs = uiview.load_preferences()
    all_rows = uiview.load_queue()
    rows, _excluded = uiview.filter_queue(all_rows, prefs)
    states = load_channel_states(_db_path, _target_channel)
    rows = uiview.sort_queue(rows, states)

    due: list[dict[str, Any]] = []
    for r in rows:
        d = uiview.days_left(r.get("deadline"))
        if d is not None and 0 <= d <= 3:
            due.append(r)
    waiting = [r for r in rows if uiview.followup_hint(r)]

    profile = actions["load"]()
    pqueue = build_queue(profile)
    health = health_metrics(pqueue)
    degraded = [s for s in states if s["state"] in ("degraded", "outage")]

    bits: list[str] = []
    if due:
        bits.append(f"{len(due)} closing within 3d")
    if waiting:
        bits.append(f"{len(waiting)} waiting on a reply")
    if pqueue.needs_human:
        bits.append(f"{pqueue.needs_human} facts to decide")
    if degraded:
        bits.append(f"{len(degraded)} channel degraded")
    _section("Today", " · ".join(bits) or "nothing waiting")

    with ui.row().classes("items-center gap-2 q-mb-md"):
        ui.label(f"{len(rows)} in your queue · "
                 f"{health['total_claims']} facts · "
                 f"{health['auto']} auto-accepted").classes("text-xs text-gray-600")
        ui.space()
        ui.button("+ add a JD",
                  on_click=lambda: _add_jd_dialog(
                      lambda: ui.navigate.reload())).props("flat")
        ui.button("+ discover",
                  on_click=lambda: _discover_dialog(
                      lambda: ui.navigate.reload())).props("flat")
        ui.button("+ add documents",
                  on_click=lambda: _ingest_dialog(actions)).props("flat")

    for s in degraded[:2]:
        with ui.card().classes("w-full q-mb-sm"):
            with ui.row().classes("items-center gap-2 q-mb-none"):
                ui.label(f"Channel degraded: {s['channel']}").classes("text-subtitle2")
                ui.badge(s["state"], color="warning")
            ui.label(f"Submissions on this channel may be slow · since {s['since']}"
                     ).classes("text-sm text-gray-600")

    # ① the reason you opened APA: something is about to close
    if due:
        ui.label("closing soon — act on these first").classes(
            "text-xs text-grey-7 q-mb-none")
        for r in due[:6]:
            _deadline_card(r, states)

    # ② you already applied; the ball is in their court
    if waiting:
        ui.label("waiting on a reply").classes("text-xs text-grey-7 q-mb-none")
        for r in waiting[:6]:
            _waiting_card(r)

    # ③ the queue keeps going: without this the page ends in ~500px of nothing
    due_ids = {r.get("jd_id") for r in due}
    nxt = [r for r in rows if r.get("jd_id") not in due_ids][:4]
    if nxt:
        ui.label("next up").classes("text-xs text-grey-7 q-mb-none")
        for r in nxt:
            with ui.card().classes("w-full q-pa-sm q-mb-xs"):
                ui.label(uiview.job_title(r.get("jd_id", ""))).classes(
                    "text-sm leading-tight q-mb-none")
                ui.label(_deadline_text(r)).classes("text-xs text-grey-7")

    # ④ facts only when there is something genuinely to arbitrate
    if pqueue.decide:
        ui.label("facts that need your call").classes(
            "text-xs text-grey-7 q-mb-none")
        for routed in pqueue.decide:
            _decision_card(actions, routed)

    if not due and not waiting and not pqueue.decide:
        with ui.card().classes("w-full items-start q-pa-md"):
            if not rows:
                ui.label("Nothing in your queue yet").classes("text-subtitle1")
                ui.label("Find a job posting, then work it end to end."
                         ).classes("text-sm text-gray-600")
                with ui.row().classes("gap-2"):
                    ui.button("+ add a JD",
                              on_click=lambda: _add_jd_dialog(
                                  lambda: ui.navigate.reload())).props("flat")
                    ui.button("+ discover",
                              on_click=lambda: _discover_dialog(
                                  lambda: ui.navigate.reload())).props("flat")
            else:
                ui.label("Nothing is urgent today").classes("text-subtitle1")
                ui.label(f"{len(rows)} in your queue · no deadline within 3d · "
                         f"nothing to follow up").classes("text-sm text-gray-600")
            ui.button("open your queue →",
                      on_click=lambda: ui.navigate.to("/queue")).props("flat")


def _deadline_card(row: dict[str, Any], states: dict[str, str]) -> None:
    """A job whose deadline is close — the one thing that must be seen first."""
    from nicegui import ui

    from . import uiview

    days = uiview.days_left(row.get("deadline")) or 0
    with ui.card().classes("w-full q-mb-sm " +
                           ("border-red-8" if days <= 1 else "border-orange-8")):
        ui.label(uiview.job_title(row.get("jd_id", ""))).classes(
            "text-subtitle2 leading-tight q-mb-none")
        with ui.row().classes("items-center gap-2 q-mb-none"):
            ui.label(row.get("jd_id", "?")).classes(
                "text-xs font-mono text-grey-6")
            ui.badge("closes today" if days == 0 else f"{days}d left",
                     color="negative" if days <= 1 else "warning")
            for text, lvl in uiview.row_badges(row, states):
                if text.startswith("closes"):
                    continue        # same fact, already shown by the badge
                color = {"urgent": "text-negative", "warn": "text-orange-9"}.get(
                    lvl, "text-grey-7")
                ui.label(text).classes(f"text-xs {color}")
        ui.label(_deadline_text(row)).classes("text-sm text-gray-600")
        ui.button("open in queue →",
                  on_click=lambda: ui.navigate.to("/queue")).props("flat dense")


def _waiting_card(row: dict[str, Any]) -> None:
    """A submitted application: show the suggestion, never send anything."""
    from nicegui import ui

    from . import uiview

    hint = uiview.followup_hint(row)
    with ui.card().classes("w-full q-mb-sm"):
        ui.label(uiview.job_title(row.get("jd_id", ""))).classes(
            "text-subtitle2 leading-tight q-mb-none")
        with ui.row().classes("items-center gap-2 q-mb-none"):
            ui.label(row.get("jd_id", "?")).classes(
                "text-xs font-mono text-grey-6")
            ui.badge("submitted", color="positive")
        ui.label(hint or "").classes("text-sm text-gray-600")
        ui.label("suggestion only — you decide whether to follow up"
                 ).classes("text-xs text-grey-6")


def _sources_of(routed: Any) -> list[tuple[str, str]]:
    """Per-source snippets for the left column."""
    claim = routed.claim
    out = [(p.source_file, p.snippet or claim.text) for p in claim.provenance]
    out += [(p.source_file, p.snippet) for p in claim.rejected_sources
            if p.extractor == "rejected-in-reingest"]
    return out


def _decision_card(actions: dict[str, Any], routed: Any) -> None:
    """One decision: your source text on the left, one specific question right.

    No "skip" button — on a decision-class item that is just a rubber-stamp
    escape hatch. Left column follows the Unstructured pattern:
    you cannot judge what you cannot check against the source.
    """
    from nicegui import ui

    field_name = routed.field
    with ui.card().classes("w-full q-mb-sm"):
        with ui.row().classes("items-center gap-2 q-mb-none"):
            ui.label(field_name).classes("text-subtitle2 text-weight-medium")
            ui.badge(routed.reason, color="warning").classes("text-xs")
        with ui.row().classes("items-stretch q-gutter-sm"):
            with ui.column().classes("col-6"):
                ui.label("your documents say").classes("text-xs text-gray-600 q-mb-none")
                for src, snippet in _sources_of(routed):
                    ui.label(f"{src}: {_short(snippet, 140)}").classes(
                        "text-sm q-mb-xs bg-green-1 q-pa-xs rounded-borders")
            with ui.column().classes("col-6"):
                ui.label("which is right?").classes("text-xs text-gray-600 q-mb-none")
                options = routed.options() or [routed.claim.text, ""]
                for i, opt in enumerate(options, start=1):
                    text = f"{i} · " + (_short(opt) if opt else "not stated — add it")
                    ui.button(text, on_click=lambda o=opt: _decide(
                        actions, field_name, o)).classes("w-full").props("flat")


def _decide(actions: dict[str, Any], field_name: str, chosen: str) -> None:
    """Record a decision and persist immediately (UI crash loses nothing)."""
    from nicegui import ui

    profile = actions["load"]()
    claim = profile.claims.get(field_name)
    if claim is None:
        ui.notify(f"no such field: {field_name}", type="negative")
        return
    try:
        if claim.status.value == "conflict":
            take = "existing" if chosen == claim.text else "incoming"
            actions["resolve"](profile, field_name, take)
        elif not chosen.strip():
            actions["needs_human"](profile, field_name)
        else:
            actions["edit"](profile, field_name, chosen)
        actions["record"]("decide", field_name, claim.text, chosen, by="ui")
        actions["save"](profile)
        ui.notify("recorded", type="positive")
        ui.navigate.reload()
    except (KeyError, ValueError) as exc:
        ui.notify(str(exc), type="negative")


# ================= screen 2: Queue =================


def _job_row(r: dict[str, Any], selected: dict[str, str], pick, states) -> None:
    """A left-column row (selectable; submission status visible - tracking layer F12-min).

    Soft status markers (docs/22 §5.4: no emoji, colour + text only): urgent =
    deadline <=7d wins the tie-break; warn = channel unhealthy / far out (mark
    only, never reject); info = draft (clock runs, no chasing).
    """
    from nicegui import ui

    from . import uiview

    today = _is_today(r)
    sub = r.get("submission") or {}
    done = (sub.get("status") == "submitted"
            or str(r.get("state", "")).lower() in ("submitted", "done"))
    active = selected["id"] == r.get("jd_id")
    cls = ("q-pa-sm q-mb-xs rounded-borders border-l-2px cursor-pointer "
           + ("bg-red-1 border-red-8" if today else "border-transparent")
           + (" opacity-50" if done else "")
           + (" bg-blue-1" if active else ""))
    with ui.element("div").classes(cls).on("click", pick):
        # Title first: GOV-SYNTH00001 alone tells a human nothing
        ui.label(uiview.job_title(r.get("jd_id", ""))).classes(
            "text-sm text-weight-medium q-mb-none leading-tight")
        ui.label(r.get("jd_id", "?")).classes(
            "text-xs text-grey-6 font-mono q-mb-none")
        bits = [b for b in (r.get("channel", ""),
                            str(r.get("deadline") or "open-ended"),
                            states.get(r.get("channel", ""), ""),
                            "submitted" if done else "") if b]
        ui.label(" · ".join(bits)).classes("text-xs text-grey-7 q-mb-none")
        with ui.row().classes("gap-1 q-mt-none"):
            for text, lvl in uiview.row_badges(r, states):
                color = {"urgent": "text-red-9", "warn": "text-orange-9",
                         "info": "text-grey-7"}[lvl]
                ui.label(text).classes(
                    f"text-xs {color} q-mb-none text-weight-medium")


def _draft_panel(head: dict[str, Any], analysis: dict[str, Any]) -> None:
    """"Generate materials" expands in place (agent finishes, human takes over).

    Iron-rule copy: **you type the fields yourself, I do not fill them in for
    you** (division-of-labor red line). Packing is deterministic and runnable
    (no-new-claims, zero LLM).
    """
    from nicegui import ui

    from . import uiview

    ui.label("Resume and cover letter for this job").classes(
        "text-sm text-weight-medium")
    ui.label("Drafted from your confirmed facts only — packing deletes, it "
             "never adds. Edit both boxes as much as you like, then re-check."
             ).classes("text-xs text-grey-6 q-mb-sm")

    # Limit input (INC-SYNTH-1015 instance: one form capped each work-experience
    # segment at 256; it varies per job - when unknown, measure first, then
    # generate: the F11 philosophy)
    with ui.row().classes("items-end gap-2 q-mb-sm"):
        limit_in = ui.number("per-segment character limit", value=256,
                             min=1, step=16).classes("w-40")
        ui.label("measured from this form (INC-SYNTH-1015 shape) — change it "
                 "per form").classes("text-xs text-grey-6")

    from .profile.store import load as load_profile

    profile_src = uiview.experience_source_text(load_profile())
    # Editable on purpose: the human owns the final text (docs/22 §6 ⑤)
    resume_box = ui.textarea("resume").classes("w-full").props(
        "outlined dense rows=10")
    cl_box = ui.textarea("cover letter").classes("w-full").props(
        "outlined dense rows=8")
    status = ui.label("").classes("text-xs text-grey-6")

    def draft() -> None:
        if not profile_src:
            status.text = ("no confirmed experience facts yet — confirm facts "
                           "on the Profile screen first (unconfirmed facts "
                           "never enter materials)")
            return
        lim = int(limit_in.value or 0) or None
        pkg = uiview.draft_package(analysis.get("jd_file"), profile_src, lim)
        if not pkg["ok"]:
            status.text = "nothing to pack — no confirmed facts for this job yet"
            return
        resume_box.value = pkg["resume"]
        cl_box.value = pkg["cover_letter"]
        status.text = (f"drafted from {len(profile_src)} of your roles "
                       f"(main: {pkg['main_role']}) — edit, then re-check")

    ui.button("draft from your confirmed facts", on_click=draft).props(
        "flat dense")
    if not profile_src:
        # Dead-end guard: the no-new-claims rule is right (unconfirmed facts
        # must not enter materials), but an empty panel with no way forward is
        # not a design — say exactly what is missing and where to fix it.
        unconfirmed = sum(
            1 for fname, claim in getattr(load_profile(), "claims", {}).items()
            if "bullets" in fname and claim.status.value == "draft")
        with ui.card().classes("w-full q-pa-sm"):
            if unconfirmed:
                ui.label(f"{unconfirmed} of your experience bullets are still "
                         "unconfirmed").classes("text-sm text-warning")
                ui.label("Unconfirmed facts never enter materials — confirm "
                         "them on the Profile screen and the drafts appear "
                         "here.").classes("text-xs text-grey-6")
            else:
                ui.label("No experience facts in your profile yet").classes(
                    "text-sm text-grey-7")
                ui.label("Add your resume on the Today screen first."
                         ).classes("text-xs text-grey-6")
            ui.button("open your profile →",
                      on_click=lambda: ui.navigate.to("/profile")
                      ).props("flat dense")

    # ---- docs/22 §6 ④ "your turn": the hand-off block (copy is a hard
    # requirement — without it the user thinks the agent already filled the
    # form and submits empty material)
    with ui.card().classes("w-full q-pa-sm q-mt-sm"):
        ui.label("Your turn — I do not fill these fields in for you").classes(
            "text-sm text-weight-medium")
        ui.label("Forms often allow one submission only; filling them for you "
                 "would be error-prone and leaves the responsibility unclear."
                 ).classes("text-xs text-grey-6 q-mb-sm")
        for i, step in enumerate([
                "open your original resume (this form has a resume attachment slot)",
                "copy the candidates above into the matching fields",
                "come back and click re-check before you submit",
        ], 1):
            ui.label(f"{i} · {step}").classes("text-xs text-grey-8")

    # ---- docs/22 §6 ⑤ recheck/submission: re-check targets **what the human typed**,
    # not what the agent drafted (stage 11, human checkpoint 6)
    with ui.card().classes("w-full q-pa-sm q-mt-sm"):
        ui.label("Re-check what you edited").classes(
            "text-sm text-weight-medium")
        re_out = ui.column().classes("w-full")

        def recheck() -> None:
            from .adapt import FieldLimit, review_final
            re_out.clear()
            src = "\n".join(profile_src.values())
            if not src.strip():
                with re_out:
                    ui.label("no confirmed experience facts yet — confirm facts "
                             "on the Profile screen first").classes(
                                 "text-sm text-grey-6")
                return
            with re_out:
                for name, box in (("resume", resume_box),
                                  ("cover letter", cl_box)):
                    raw = (box.value or "").strip()
                    if not raw:
                        continue
                    # placeholders are lines the human is told to write —
                    # they are not claims and must not be flagged as invented
                    txt = raw
                    for ph in (uiview.CL_OPENING, uiview.CL_CLOSING):
                        txt = txt.replace(ph, "")
                    ui.label(f"{name}: {len(raw)} chars").classes(
                        "text-xs text-grey-7")
                    # a resume/CL is a whole document here, but document-level
                    # MUST_HAVE would fire on every fragment, so it stays off
                    rr = review_final(txt, FieldLimit(
                        field_name=name, limit=None, source="unverified"))
                    if rr.passed:
                        ui.badge("re-check passed — no rule fired",
                                 color="positive")
                    else:
                        ui.badge("re-check found problems", color="negative")
                        for _ln, msg in rr.findings:
                            ui.label(f"· {msg}").classes("text-xs text-red-9")
                    # no-new-claims: anything you invented shows up as insert
                    invented = [seg for op, seg in uiview.pack_diff(src, txt)
                                if op == "insert"]
                    if invented:
                        ui.label("new claims — these are NOT in your confirmed "
                                 "facts, so they were invented, not packed:"
                                 ).classes("text-xs text-warning")
                        for s in invented[:10]:
                            ui.label(f"· {_short(s, 100)}").classes(
                                "text-xs text-warning")
                    else:
                        ui.label("every sentence traces back to your confirmed "
                                 "facts").classes("text-xs text-grey-6")

        with ui.row().classes("gap-2 items-center"):
            ui.button("re-check what I edited", on_click=recheck
                      ).props("flat dense")
        url = str(head.get("source_url") or "").strip()
        if url:
            # ui.link with new_tab is a real <a target=_blank>; ui.open was
            # renamed ui.navigate.to in NiceGUI 3.x and its new_tab can be
            # blocked by the browser.
            ui.link("open the application page", url, new_tab=True).classes(
                "text-sm text-primary")
        else:
            ui.label("no source URL recorded for this job").classes(
                "text-xs text-grey-6")


def _detail_real(head: dict[str, Any], states: dict[str, str], container,
                 draft_ref: dict[str, Any]) -> None:
    """Middle column: real analysis + real compliance + draft panel (honest empty state when there is no data)."""
    from nicegui import ui

    from . import uiview

    jd_id = head.get("jd_id", "?")
    with container:
        ui.label(uiview.job_title(jd_id, max_len=96)).classes(
            "text-h6 q-mb-none leading-tight")
        ui.label(f"{jd_id} · {head.get('channel', '')}"
                 ).classes("text-xs font-mono text-grey-6 q-mt-none")
        ui.label(_deadline_text(head)).classes("text-sm text-grey-7 q-mt-none")

        # ---- what the agent found (real data only) ----
        ui.label("what the agent found").classes(
            "text-xs text-grey-7 q-mb-none q-mt-sm")
        a = uiview.load_job_analysis(jd_id)
        if a["analyzed"]:
            if a["similar_top"]:
                with ui.row().classes("gap-2 q-mb-sm"):
                    ui.label("similar past packages:").classes("text-xs text-grey-7")
                    for s in a["similar_top"][:5]:
                        ui.badge(s, color="grey-8")
            if a["decision"]:
                d = a["decision"]
                ui.label(f"you decided: {d['action']} — {_short(d['reason'], 60)} "
                         f"({d['by']}, {_short(d['at'], 10)})"
                         ).classes("text-sm q-mb-none")
            if a["needs_human"]:
                ui.label(f"extraction needs a human: {_short(a['needs_human'], 120)}"
                         ).classes("text-sm text-warning")
        else:
            ui.label("not analyzed yet").classes("text-sm text-grey-7 q-mb-none")
            jd_file = a.get("jd_file")
            if jd_file:
                # Show the file NAME, not the whole local path: absolute paths
                # (and their Chinese folder names) are terminal content.
                ui.label(f"JD file: {Path(jd_file).name}").classes(
                    "text-xs text-grey-6")
                with ui.expansion("how to run the analysis in the terminal"
                                  ).classes("w-full"):
                    ui.label(f'apa agent run --jd "{jd_file}"').classes(
                        "text-xs font-mono text-grey-6")
            else:
                ui.label("link a JD file (add a JD by URL, or set "
                         "APA_SINGAPORE_ROOT)").classes("text-xs text-grey-6")

        # ---- checks on OUR material (real compliance run) ----
        ui.label("checks on your material for this job").classes(
            "text-xs text-grey-7 q-mb-none q-mt-sm")
        if a.get("our_material"):
            comp = uiview.run_compliance(a["our_material"])
            if comp.get("error"):
                ui.label(f"check failed to run: {comp['error']}"
                         ).classes("text-sm text-warning")
            elif comp["ok"]:
                ui.badge(f"{comp['n_rules']} rules pass", color="positive")
            else:
                # Collapsed by default: five open red lines is a wall of red
                # that tells the eye nothing. The count carries the signal.
                ui.badge(f"{comp['hard_count']} need fixing", color="negative")
                with ui.expansion(f"show the {len(comp['findings'])} lines"
                                  ).classes("w-full"):
                    for f in sorted(comp["findings"],
                                    key=lambda x: not x["hard"])[:6]:
                        tag = "hard" if f["hard"] else "check"
                        loc = f"L{f['line']}" if f["line"] else "doc-level"
                        ui.label(f"[{tag}] [{f['code']}] {f['desc']}  {loc}"
                                 ).classes("text-xs font-mono q-mb-none "
                                           + ("text-red-9" if f["hard"]
                                              else "text-orange-9"))
        else:
            ui.label("no material in this package yet — draft materials below "
                     "prepares candidates").classes("text-sm text-grey-6")

        # ---- keyword coverage (four states, heuristic — no LLM) ----
        # no icon= : the icon font is not bundled (it renders as a tofu box)
        with ui.expansion("keyword coverage").classes("w-full q-mt-sm"):
            ui.label("heuristic scan, no LLM — a reading aid, not a "
                     "verdict. Check every line against the JD yourself."
                     ).classes("text-xs text-grey-6 q-mb-sm")
            jd_file = a.get("jd_file")
            if not jd_file:
                ui.label("no JD file linked for this job yet"
                         ).classes("text-sm text-grey-7")
            else:
                from .profile.store import load as load_profile

                try:
                    jd_text = Path(jd_file).read_text(encoding="utf-8")
                except OSError as exc:
                    jd_text = ""
                    ui.label(f"cannot read JD: {exc}"
                             ).classes("text-sm text-warning")
                material = ""
                if a.get("our_material"):
                    try:
                        material += Path(
                            a["our_material"]).read_text(encoding="utf-8")
                    except OSError:
                        pass
                material += "\n".join(
                    uiview.experience_source_text(load_profile()).values())
                if not material.strip():
                    ui.label("nothing to compare against yet — no cover "
                             "letter in this package and no confirmed "
                             "experience facts (confirm facts on the Profile "
                             "screen first)").classes("text-sm text-grey-7")
                else:
                    cov = uiview.keyword_coverage(jd_text, material)
                    _STATE_STYLE = {
                        "covered": "text-positive",
                        "synonym_only": "text-info",
                        "missing_have": "text-warning",
                        "missing_gap": "text-negative",
                    }
                    if not any(cov.values()):
                        ui.label("no keyword candidates found in this JD"
                                 ).classes("text-sm text-grey-7")
                    for state, kws in cov.items():
                        if not kws:
                            continue
                        color = _STATE_STYLE[state]
                        with ui.row().classes("gap-2 items-baseline"):
                            ui.label(state.replace('_', '-')
                                     ).classes(
                                         f"text-xs {color} text-weight-bold")
                            ui.label(", ".join(kws[:10])
                                     + (" …" if len(kws) > 10 else "")
                                     ).classes("text-xs text-grey-8")

        # ---- draft materials ----
        with ui.expansion("draft materials").classes("w-full q-mt-sm") as exp:
            _draft_panel(head, a)
        draft_ref["exp"] = exp          # opened by the right-column button


def _actions_real(head: dict[str, Any], column, rerender,
                  draft_ref: dict[str, Any]) -> None:
    """Right column: three actions + tracking (F12-min: user write-back is the sole authority)."""
    from nicegui import ui

    from . import uiview

    jd_id = head.get("jd_id", "?")
    with column:
        ui.label("next").classes("text-xs text-grey-7 q-mb-none")
        # One primary action, two secondary ones: three equal-weight solid blue
        # bars give the eye nothing to choose from (Quasar also uppercases
        # button labels by default, hence no-caps everywhere).
        ui.button("Draft materials",
                  on_click=lambda: draft_ref.get("exp") and
                  setattr(draft_ref["exp"], "value", True)
                  ).classes("w-full").props("no-caps")
        ui.button("Not this one", on_click=lambda: _hitl(head, "waive")
                  ).classes("w-full").props("outline no-caps")
        ui.button("Later", on_click=lambda: _hitl(head, "defer")
                  ).classes("w-full").props("flat no-caps")

        # ---- tracking (phase 12 / F12-min) ----
        a = uiview.load_job_analysis(jd_id)
        sub = a.get("submission") or {}
        with ui.column().classes("w-full q-mt-md q-gutter-xs"):
            ui.label("tracking").classes("text-xs text-grey-7 q-mb-none")
            if a.get("submitted_signal") and sub.get("status") != "submitted":
                ui.label("package has a submitted marker — record it below to "
                         "keep the clock honest").classes("text-xs text-grey-6")
            if sub.get("status") == "submitted":
                ui.badge(f"submitted {sub.get('submitted_at', '?')}",
                         color="positive")
                hint = uiview.followup_hint(
                    {"submission": sub, "deadline": head.get("deadline")})
                if hint:
                    ui.label(hint).classes("text-xs text-grey-7")
                if len(sub.get("followups", [])) < uiview.FOLLOWUP_MAX:
                    def _follow() -> None:
                        r = uiview.record_followup(jd_id)
                        ui.notify(("follow-up recorded"
                                   if r["ok"] else r["error"]),
                                  type="positive" if r["ok"] else "warning")
                        rerender()
                    ui.button("record a follow-up", on_click=_follow
                              ).props("flat dense")
            else:
                def _mark() -> None:
                    uiview.mark_submitted(jd_id)
                    ui.notify(f"recorded: you submitted {jd_id}", type="positive")
                    rerender()
                ui.button("mark submitted", on_click=_mark).props("flat dense")
                ui.label("your write-back is the only authority — the ledger "
                         "still updates through the ops channel"
                         ).classes("text-xs text-grey-6")


def _add_jd_dialog(rerender) -> None:
    """Phase-4/5 discovery: paste a URL (preferred - traceable); the browser renders it, then it is stored and queued."""
    from nicegui import ui

    from . import uiview

    with ui.dialog() as dlg, ui.card():
        ui.label("Add a JD by URL").classes("text-h6")
        ui.label("Paste a posting URL — a real browser renders it (SPA-safe), "
                 "the text lands in your queue with its source URL attached. "
                 "Blocked pages are reported, never silently queued."
                 ).classes("text-sm text-gray-600")
        url_in = ui.input("posting URL").classes("w-96")
        status = ui.label("").classes("text-xs text-grey-6")

        async def run() -> None:
            status.text = "rendering in a browser (up to ~20s)..."
            from nicegui import run as nrun
            r = await nrun.io_bound(uiview.add_jd_by_url, url_in.value.strip())
            if r["ok"]:
                dlg.close()
                ui.notify(f"added: {r['title']} ({r['chars']} chars)",
                          type="positive")
                rerender()
            else:
                status.text = f"blocked: {r['error']}"

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("cancel", on_click=dlg.close).props("flat")
            ui.button("fetch & add", color="primary", on_click=run)
    dlg.open()


def _discover_dialog(rerender) -> None:
    """Stage-4 conditional search: query -> a real browser renders the search
    page -> JD links are pulled out -> user picks which to add.

    Each picked link goes through ``add_jd_by_url`` (stage 5), so every stored
    JD keeps its source URL (traceable - the whole point of APA). The search
    page URL itself is also exposed as the origin of the find.
    """
    from nicegui import ui

    from . import uiview

    channels = [(k, v["label"]) for k, v in uiview.SEARCH_CHANNELS.items()]
    chan_map = {k: v for k, v in channels}

    with ui.dialog() as dlg, ui.card().classes("w-128 max-w-full"):
        ui.label("Discover jobs (Stage 4)").classes("text-h6")
        ui.label("Enter a query or paste a search-results page URL. A real "
                 "browser opens it (SPA-safe); JD links are pulled out and you "
                 "choose which to add. Blocked pages are reported, never silent."
                 ).classes("text-sm text-gray-600")
        with ui.row().classes("w-full gap-2"):
            chan = ui.select(chan_map, value=channels[0][0]).props("label=channel")
            query = ui.input("query (e.g. data engineer)").classes("grow")
        url_in = ui.input("or paste a search URL (optional)").classes("w-full")
        status = ui.label("").classes("text-xs text-grey-6")
        results = ui.column().classes("w-full")

        async def do_search() -> None:
            results.clear()
            status.text = "rendering the search page in a browser (up to ~20s)..."
            from nicegui import run as nrun
            r = await nrun.io_bound(
                uiview.search_jds, chan.value, query.value.strip(),
                url_in.value.strip())
            if not r["ok"]:
                status.text = f"blocked: {r['error']}"
                return
            items = r["items"]
            status.text = (f"{r['count']} JD links found"
                           + (f" from {r['search_url']}" if r.get("search_url") else ""))
            if not items:
                ui.notify("no JD links extracted - the page may need a longer "
                          "wait or a different channel", type="warning")
                return
            boxes: list[tuple[dict[str, str], Any]] = []
            for it in items:
                with results:
                    with ui.row().classes("items-center gap-2 w-full"):
                        box = ui.checkbox("", value=True)
                        lbl = ui.label(it["title"] or it["url"]).classes(
                            "text-sm grow")
                        if it["url"]:
                            lbl.tooltip(it["url"])
                        boxes.append((it, box))

            async def add_selected() -> None:
                chosen = [it for it, box in boxes if box.value]
                if not chosen:
                    ui.notify("select at least one JD", type="warning")
                    return
                added = 0
                for it in chosen:
                    ar = await nrun.io_bound(uiview.add_jd_by_url, it["url"])
                    if ar.get("ok"):
                        added += 1
                dlg.close()
                ui.notify(f"added {added} JD(s) to the queue", type="positive")
                rerender()

            with results:
                ui.button("add selected", color="primary",
                          on_click=add_selected).props("flat")

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("cancel", on_click=dlg.close).props("flat")
            ui.button("search", color="primary", on_click=do_search)
    dlg.open()


def build_queue_screen(actions: dict[str, Any]) -> None:
    """Which one to apply for: list left, evidence centre, actions right.

    Exactly three actions and no bulk-approve — a select-all box is a
    loaded gun pointed at your own quality bar.
    """
    from nicegui import ui

    from . import uiview

    _topbar("Queue")
    prefs = uiview.load_preferences()
    all_rows = uiview.load_queue()
    rows, excluded = uiview.filter_queue(all_rows, prefs)
    rows = uiview.sort_queue(rows)
    urgent = [r for r in rows
              if any(lvl == "urgent" for _, lvl in uiview.row_badges(r))]
    sub = f"{len(rows)} jobs"
    if urgent:
        sub += f" · {len(urgent)} closing within 7d"
    if excluded:
        sub += f" · {len(excluded)} hidden by your filters"
    _section("Queue", sub)

    if not all_rows:
        with ui.card().classes("w-full items-start q-pa-md"):
            ui.label("Your queue is empty").classes("text-subtitle1")
            ui.label("Add a JD by URL, or start from your profile."
                     ).classes("text-sm text-gray-600")
            with ui.row().classes("gap-2"):
                ui.button("+ add a JD",
                          on_click=lambda: _add_jd_dialog(
                              lambda: ui.navigate.reload())
                          ).props("flat")
                ui.button("+ discover",
                          on_click=lambda: _discover_dialog(
                              lambda: ui.navigate.reload())
                          ).props("flat")
                ui.button("open your profile →",
                          on_click=lambda: ui.navigate.to("/profile")).props("flat")
        return

    if not rows:
        with ui.card().classes("w-full items-start q-pa-md"):
            ui.label(f"All {len(all_rows)} jobs are hidden by your filters"
                     ).classes("text-subtitle1")
            for _, why in excluded[:6]:
                ui.label(f"· {why}").classes("text-sm text-gray-600")
            ui.button("edit filters on your profile →",
                      on_click=lambda: ui.navigate.to("/profile")).props("flat")
        return

    states = {s["channel"]: s["state"]
              for s in load_channel_states(_db_path, _target_channel)}
    rows = uiview.sort_queue(rows, states)
    selected = {"id": rows[0].get("jd_id", "")}
    draft_ref: dict[str, Any] = {}

    def rerender() -> None:
        detail.clear()
        right.clear()
        head = next((r for r in rows if r.get("jd_id") == selected["id"]), rows[0])
        _detail_real(head, states, detail, draft_ref)
        _actions_real(head, right, rerender, draft_ref)

    with ui.row().classes("items-start no-wrap full-width q-gutter-none"):
        with ui.column().classes("col-3"):
            ui.button("+ add a JD", on_click=lambda: _add_jd_dialog(rerender)
                      ).props("flat dense")
            ui.button("+ discover", on_click=lambda: _discover_dialog(rerender)
                      ).props("flat dense")
            for r in rows:
                def pick(_r=r):
                    selected["id"] = _r.get("jd_id", "")
                    rerender()
                _job_row(r, selected, pick, states)
            if excluded:
                ui.label("hidden by your filters").classes(
                    "text-xs text-grey-6 q-mt-md q-mb-none")
                for e, why in excluded[:8]:
                    ui.label(f"· {e.get('jd_id', '?')} — {why}").classes(
                        "text-xs text-grey-6")
        detail = ui.column().classes("col-6 q-px-md")
        right = ui.column().classes("col-3")
    rerender()


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _is_today(row: dict[str, Any]) -> bool:
    return str(row.get("deadline") or "").strip() == _today()


def _deadline_text(row: dict[str, Any]) -> str:
    d = str(row.get("deadline") or "").strip()
    return f"closes {d}" if d else "open-ended (no closing date published)"


def _hitl(row: dict[str, Any], action: str) -> None:
    """Append-only HITL decision (ledger still goes through the ops channel)."""
    from nicegui import ui

    try:
        record_hitl_decision(row.get("jd_id", ""), action, "")
        ui.notify(f"recorded: {action}", type="positive")
    except ValueError as exc:
        ui.notify(str(exc), type="negative")


# ================= screen 3: Profile (grouped by experience) =================

def _preferences_card() -> None:
    """Phase-3 "set preferences": hard filters (region / channels / exclusions) + soft preferences (role direction).

    Hard filters narrow the Queue left column live; exclusions get a **listed
    reason** (ai-job-search traceable semantics). Stored in the profile-side
    preferences.json - **does not change already-submitted records** (submission
    is historical fact).
    """
    from nicegui import ui

    from . import uiview

    prefs = uiview.load_preferences()
    known_channels = ["hrp", "mcf", "workable", "wd102", "wd3",
                      "greenhouse", "jobstreet", "fetched"]
    with ui.card().classes("w-full q-mb-md"):
        ui.label("what you want").classes("text-subtitle2")
        ui.label("Hard filters narrow your queue; role keywords are soft "
                 "signals for matching.").classes("text-xs text-grey-6")

        with ui.row().classes("gap-4 items-start"):
            with ui.column():
                ui.label("channels").classes("text-xs text-grey-7")
                checks: dict[str, Any] = {}
                # 2 columns: eight stacked checkboxes burned 350px of height
                # and left the right half of the card empty
                with ui.grid(columns=2).classes("gap-x-4 gap-y-0"):
                    for ch in known_channels:
                        off = (prefs.get("channels") or {}).get(ch) is False
                        checks[ch] = ui.checkbox(ch, value=not off)
            with ui.column():
                ui.label("exclude (one per line — org codes or names)"
                         ).classes("text-xs text-grey-7")
                exclude = (ui.textarea(value="\n".join(prefs.get("exclude") or []))
                           .classes("w-64").props("outlined dense rows=3"))
                roles = ui.input("role directions (comma separated)",
                                 value=", ".join(prefs.get("roles") or [])
                                 ).classes("w-64")
                salary = ui.select(["ask", "range", "none"],
                                   value=prefs.get("salary_policy", "ask"),
                                   label="salary policy").classes("w-48")

        def save() -> None:
            new = dict(prefs)
            new["channels"] = {c: box.value for c, box in checks.items()}
            new["exclude"] = [s.strip() for s in (exclude.value or "").splitlines()
                              if s.strip()]
            new["roles"] = [s.strip() for s in (roles.value or "").split(",")
                            if s.strip()]
            new["salary_policy"] = salary.value
            uiview.save_preferences(new)
            ui.notify("saved — your queue filters update immediately",
                      type="positive")

        ui.button("save filters", on_click=save).props("flat dense")


def _grouped_experiences(profile: Any) -> list[dict[str, Any]]:
    """Group facts by experience (**including unconfirmed drafts**, but flagged).

    ``materialize_experiences`` cannot be used directly: it only collects
    confirmed claims (the correct scope for downstream materials), while the
    Profile screen's job is precisely to **let people see facts awaiting
    verification**. So a separate display scope is made here and drafts are
    explicitly flagged - otherwise users would take unverified facts as settled.
    """
    groups: dict[int, dict[str, Any]] = {}
    for key, claim in profile.claims.items():
        parts = key.split(".")
        if parts[0] != "experiences" or len(parts) < 3:
            continue
        idx = int(parts[1]) if parts[1].isdigit() else 0
        slot = groups.setdefault(idx, {"org": "", "title": "", "start": "", "end": "",
                                       "facts": [], "drafts": 0, "keys": []})
        slot["keys"].append(key)     # every field of this role, for batch confirm
        attr = ".".join(parts[2:])
        if attr.startswith("bullets."):
            slot["facts"].append(claim)
            if claim.status.value == "draft":
                slot["drafts"] += 1
        elif attr in ("org", "title", "start", "end") and not slot[attr]:
            slot[attr] = claim.text
    out = []
    for idx in sorted(groups):
        slot = groups[idx]
        if not (slot["org"] or slot["title"] or slot["facts"]):
            continue
        slot["multi"] = sum(1 for c in slot["facts"]
                            if len({p.source_file for p in c.provenance}) > 1)
        slot["pinned"] = sum(1 for c in slot["facts"] if c.status.value == "manual_override")
        out.append(slot)
    return out


def _display_text(field: str, text: str) -> str:
    """Display-scope mapping: claim.text may be an enum raw value (e.g.
    work_authorization's sg_citizen); the user-visible side must map it to a
    label - same source as the CLI ``apa profile show`` (models.py's
    WorkAuthorization.label). The edit box does not use this mapping: it shows
    the stored raw value; what the human changes is what goes into the profile."""
    if field == "identity.work_authorization":
        from .profile.models import WorkAuthorization
        try:
            return WorkAuthorization(text).label
        except ValueError:
            return text                      # non-enum value (human-edited) -> as-is
    return text


def _basics_claims(profile: Any) -> list[dict[str, Any]]:
    rows = []
    for k, c in sorted(profile.claims.items()):
        if not k.startswith("identity."):
            continue
        rows.append({"field": k, "text": _display_text(k, c.text),
                     "draft": c.status.value == "draft"})
    return rows


def build_profile_screen(actions: dict[str, Any]) -> None:
    """Are my facts right? Grouped by experience — not 25 loose fields."""
    from nicegui import ui

    from .profile.router import build_queue

    _topbar("Profile")
    profile = actions["load"]()
    queue = build_queue(profile)
    docs = {p.source_file for c in profile.claims.values() for p in c.provenance}
    drafted = sum(1 for c in profile.claims.values() if c.status.value == "draft")
    sub = f"{queue.total} facts · {len(docs)} documents"
    if drafted:
        sub += f" · {drafted} not yet confirmed"
    _section("Profile", sub)

    with ui.row().classes("items-center gap-2 q-mb-md"):
        ui.button("add a document", on_click=lambda: _ingest_dialog(actions)).props("flat")
        ui.button("edit a fact",
                  on_click=lambda: ui.navigate.to("/profile/all")).props("flat")
        ui.space()
        ui.link(f"all {queue.total} →", "/profile/all").classes("text-sm")

    if not profile.claims:
        with ui.card().classes("w-full items-start q-pa-md"):
            ui.label("No facts yet").classes("text-subtitle1")
            ui.label(f"Drop docx / pdf / md into {_inbox_dir()} — you never type "
                     "them in by hand").classes("text-sm text-gray-600")
        return

    _preferences_card()
    for grp in _grouped_experiences(profile):
        bullets = grp["facts"]
        with ui.card().classes("w-full q-mb-sm"):
            with ui.row().classes("items-baseline justify-between q-mb-none"):
                ui.label(f"{grp['org'] or '(unnamed)'} · {grp['title']}"
                         ).classes("text-subtitle2")
                period = grp["start"] or "?"
                if grp["end"]:
                    period += f" – {grp['end']}"
                ui.label(period).classes("text-xs text-grey-7")
            bits = [f"{len(bullets)} facts"]
            if grp["multi"]:
                bits.append(f"{grp['multi']} corroborated by two documents")
            if grp["drafts"]:
                bits.append(f"{grp['drafts']} not yet confirmed")
            if grp["pinned"]:
                bits.append(f"{grp['pinned']} you edited")
            ui.label(" · ".join(bits)).classes("text-sm text-grey-7 q-mb-none")
            for b in bullets[:3]:
                pending = b.status.value == "draft"
                ui.label(_short(b.text, 88)).classes(
                    "text-xs q-mb-none border-l-2px q-pl-sm "
                    + ("border-orange-8 text-grey-8" if pending
                       else "border-positive text-grey-9"))
            if grp["drafts"]:
                # One deliberate click per role, not 25 stamps: the facts are
                # shown right above, so this is a decision, not a rubber stamp.
                n_pending = grp["drafts"]

                def _confirm(_grp=grp) -> None:
                    from .profile.merge import confirm_claim
                    for fname in _grp["keys"]:
                        confirm_claim(profile, fname)
                    actions["save"](profile)
                    ui.notify(f"confirmed this role ({len(_grp['keys'])} facts)",
                              type="positive")
                    ui.navigate.reload()

                ui.button(f"confirm this role ({n_pending} pending)",
                          on_click=_confirm).props("flat dense color=primary")
                ui.label("confirming makes these facts usable in your drafts — "
                         "read them above first").classes("text-xs text-grey-6")

    basics = _basics_claims(profile)
    if basics:
        with ui.card().classes("w-full q-mb-md"):
            ui.label("Basics").classes("text-subtitle2 q-mb-none")
            ui.label(" · ".join(b["text"] for b in basics)).classes(
                "text-sm q-mb-none")
            unconfirmed = sum(1 for b in basics if b["draft"])
            ui.label("taken from your documents"
                     + (f" · {unconfirmed} not yet confirmed" if unconfirmed else " · confirmed")
                     ).classes("text-xs text-grey-7")


# ================= secondary: all facts (power-user mode) =================

def build_profile_all(actions: dict[str, Any]) -> None:
    """Flat table = power-user mode; per-item editing lives here, not on /profile."""
    from nicegui import ui

    from .profile.router import route_all

    _topbar("Profile")
    routed = route_all(actions["load"]().claims.values())
    _section("All facts", f"{len(routed)} · sorted by review route (decide first)")

    table = ui.table(
        columns=[{"name": "field", "label": "field", "field": "field",
                  "align": "left"},
                 {"name": "route", "label": "route", "field": "route"},
                 {"name": "text", "label": "value", "field": "text"},
                 {"name": "sources", "label": "sources", "field": "sources"}],
        rows=[{"field": r.field, "route": r.route.value,
               "text": _short(_display_text(r.field, r.claim.text), 70),
               "sources": ", ".join(sorted({p.source_file for p in r.claim.provenance}))}
              for r in routed],
        row_key="field", selection="single")
    table.classes("w-full")
    ui.button("edit selected", on_click=lambda: _edit_selected(actions, table)
              ).props("flat dense")


def _edit_selected(actions: dict[str, Any], table: Any) -> None:
    from nicegui import ui

    sel = getattr(table, "selected", None) or []
    if not sel:
        ui.notify("pick a row first", type="warning")
        return
    field_name = sel[0] if isinstance(sel[0], str) else next(iter(sel[0].values()))
    profile = actions["load"]()
    claim = profile.claims.get(field_name)
    if claim is None:
        ui.notify(f"no such field: {field_name}", type="negative")
        return
    with ui.dialog() as dlg, ui.card():
        ui.label(f"Edit {field_name}").classes("text-h6")
        ui.label("This pins the field — re-ingesting your documents will never "
                 "overwrite it.").classes("text-xs text-gray-600")
        box = ui.textarea(value=claim.text).classes("w-96")
        reason = ui.input("reason (optional)").classes("w-96")

        def commit() -> None:
            try:
                actions["edit"](profile, field_name, box.value, reason.value)
                actions["record"]("edit", field_name, claim.text, box.value,
                                  reason=reason.value, by="ui")
                actions["save"](profile)
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            dlg.close()
            ui.notify("pinned — re-ingest will not touch it", type="positive")
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("cancel", on_click=dlg.close).props("flat")
            ui.button("save", color="primary", on_click=commit)
    dlg.open()


# ================= shared: ingest dialog =================

def _ingest_dialog(actions: dict[str, Any]) -> None:
    from nicegui import ui

    inbox = _inbox_dir()
    with ui.dialog() as dlg, ui.card():
        ui.label("Add documents").classes("text-h6")
        ui.label(f"Drop .docx / .pdf / .txt / .md here or into {inbox} — you never "
                 "type them in, the agent extracts and you review."
                 ).classes("text-sm text-gray-600")
        ui.label("Scanned PDFs are not supported (no OCR) — they fail loudly "
                 "rather than producing an empty profile"
                 ).classes("text-xs text-gray-600")

        async def on_upload(e) -> None:
            """Drag/select files -> land in inbox -> ingest immediately (phase-1 entry point 2)."""
            await e.file.save(inbox / e.file.name)
            summary, failures = actions["ingest_dir"](inbox)
            msg = f"extracted {summary.get('extracted', 0)} facts"
            for path, why in failures[:3]:
                msg += f" | skipped {path.name}: {_short(why, 70)}"
            dlg.close()
            ui.notify(msg, type="positive" if summary.get("extracted") else "warning",
                      timeout=7000)
            ui.navigate.reload()

        ui.upload(on_upload=on_upload, multiple=True, auto_upload=True
                  ).classes("w-full max-w-lg").props('color="grey-8" flat bordered')
        folder = ui.input("or a folder path").classes("w-96")

        def run() -> None:
            target = Path(folder.value.strip()) if folder.value.strip() else inbox
            summary, failures = actions["ingest_dir"](target)
            msg = f"extracted {summary.get('extracted', 0)} facts"
            for path, why in failures[:3]:
                msg += f" | skipped {path.name}: {_short(why, 70)}"
            dlg.close()
            ui.notify(msg,
                      type="positive" if summary.get("extracted") else "warning",
                      timeout=7000)
            ui.navigate.reload()

        with ui.row().classes("justify-end w-full gap-2"):
            ui.button("cancel", on_click=dlg.close).props("flat")
            ui.button("ingest", color="primary", on_click=run)
    dlg.open()



# ================= screen: Settings (system health + LLM config) =================

def build_settings(actions: dict[str, Any]) -> None:
    """System health check + LLM configuration (ApplyPilot doctor mode: what is missing, with the fix).

    Write scope: backend / base_url / model / api_key are stored in
    ``data/settings.json`` (gitignored); **env variables take precedence** - this
    file is only the default. Keys never enter git.
    """
    from nicegui import ui

    from . import uiview

    _topbar("Settings")
    _section("Settings", "what is connected, what is missing, what to do about it")

    h = uiview.settings_health()
    sov = h["sovereignty"]
    with ui.row().classes("items-center gap-2 q-mb-md"):
        ui.badge(sov["label"], color=sov["color"])
        ui.label(f"backend in use: {h['backend']}").classes("text-sm text-grey-7")

    for item in h["items"]:
        color = {"ok": "positive", "off": "grey-7", "opt-in": "warning"}.get(
            item["status"], "grey-7")
        with ui.row().classes("items-start gap-2 w-full q-mb-xs"):
            ui.badge(item["status"], color=color)
            with ui.column().classes("gap-0"):
                ui.label(item["name"]).classes("text-sm")
                if item.get("hint"):
                    ui.label(item["hint"]).classes("text-xs text-grey-6")

    ui.label("LLM backend").classes("text-subtitle2 q-mt-md")
    cur = h.get("settings", {})
    with ui.card().classes("w-full q-pa-md"):
        backend = ui.select(["auto", "workbuddy", "cloud", "local"],
                            value=cur.get("backend", "auto"), label="backend"
                            ).classes("w-full")
        base_url = ui.input("base URL", value=cur.get("base_url", "")
                            ).classes("w-full").props(
            "placeholder=http://127.0.0.1:8080/v1")
        model = ui.input("model", value=cur.get("model", "")).classes(
            "w-full").props("placeholder=qwen2.5-1.5b-instruct")
        api_key = ui.input("API key (stored only in data/settings.json, "
                           "never in git)",
                           value=cur.get("api_key", ""),
                           password=True).classes("w-full")

        def save() -> None:
            uiview.save_user_settings({
                "backend": backend.value, "base_url": base_url.value.strip(),
                "model": model.value.strip(), "api_key": api_key.value.strip()})
            ui.notify("saved — environment variables still override this file",
                      type="positive")

        ui.button("save", color="primary", on_click=save).props("flat")
        ui.label("Priority: APA_LLM_BACKEND (env) > this file > auto-probe. "
                 "Cloud mode double-switch still applies "
                 "(APA_ALLOW_CLOUD_LLM=1).").classes("text-xs text-grey-6")

    # Collapsed, not deleted: the paths are real information (and the workspace
    # folder name is Chinese - it stays exactly as it is).
    with ui.card().classes("w-full q-pa-md q-mt-md"):
        with ui.expansion("Paths").classes("w-full"):
            rows = [("data dir", str(config.DATA_DIR)),
                    ("job workspace (APA_SINGAPORE_ROOT)",
                     str(config.SINGAPORE_ROOT or "not set")),
                    ("JD packages", str(uiview.packages_dir() or "not set"))]
            for k, v in rows:
                ui.label(f"{k}: {v}").classes("text-xs font-mono text-grey-7")


# ================= wiring =================

def build_app(db_path: Path, queue_path: Path, target_channel: dict[str, str]):
    """Build the three-screen app (Today / Queue / Profile).

    NiceGUI 3.x: pages register via ``@ui.page``; the app object comes from
    ``nicegui.app`` (module-level ``ui.app`` was removed in 3.x).
    """
    from nicegui import app, ui

    # Quasar uppercases every button label by default, which turns ordinary
    # actions into shouting ("NOT THIS ONE", "SAVE FILTERS"). Sentence case is
    # the product voice; one global rule beats no-caps on 40 call sites.
    ui.add_head_html(
        "<style>.q-btn{text-transform:none!important;letter-spacing:0}</style>",
        shared=True)

    global _db_path, _queue_path, _target_channel
    _db_path, _queue_path, _target_channel = db_path, queue_path, target_channel
    actions = profile_actions()

    @ui.page("/")
    def _today():
        build_today(actions)

    @ui.page("/queue")
    def _queue():
        build_queue_screen(actions)

    @ui.page("/profile")
    def _profile():
        build_profile_screen(actions)

    @ui.page("/profile/all")
    def _profile_all():
        build_profile_all(actions)

    @ui.page("/settings")
    def _settings():
        build_settings(actions)

    return app


def run(db_path: Path, queue_path: Path, port: int = 8501) -> None:
    """Start the console. Binds 127.0.0.1 only — never 0.0.0.0."""
    import os

    os.environ.setdefault("DO_NOT_TRACK", "1")
    target_channel: dict[str, str] = {}
    if Path(config.PROBE_TARGETS_PATH).exists():
        try:
            targets = json.loads(
                Path(config.PROBE_TARGETS_PATH).read_text(encoding="utf-8"))["targets"]
            for t in targets:
                target_channel.setdefault(t.get("channel", ""), t.get("channel", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    from nicegui import ui

    build_app(db_path, queue_path, target_channel)
    ui.run(host="127.0.0.1", port=port, title="APA console",
           reload=False, show=False)
