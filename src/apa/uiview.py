"""UI data-assembly layer (pure functions, **does not import nicegui**).

Discipline (thin-pointer rule): the UI is only a presentation layer - all
business logic goes through the existing packages (profile / adapt /
compliance / f10 / llm). This module assembles the "real data" the UI needs
into unit-testable pure functions - **no fake data; when there is nothing,
say so honestly** (empty-state discipline).

Covers phases 1 and 2 of the gap plan:
- load_job_analysis   queue middle column made real (agent_runs + extraction_review + queue)
- run_compliance      compliance on the fly (deterministic rules, zero LLM)
- draft_materials     adapt packing (no-new-claims, zero LLM)
- preferences         phase-3 preferences (hard filter + soft preferences)
- user settings       phase-0 settings (LLM backend / Chrome / path health check + persistence)
- search_jds          phase-4 conditional search (query -> browser renders the search page -> JD links extracted)
- add_jd_by_url       phase-4/5 discovery (browser rendered fetch -> into the library -> into the queue)
- submission lifecycle  phase-12 tracking (F12-min: user write-back is the sole authority)
"""

from __future__ import annotations

import json
import re
import html as _html
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config
from .adapt import FieldLimit, fit_to_budget, review_final

# ---------------------------------------------------------------- paths


def packages_dir() -> Optional[Path]:
    """Real submission-package directory (where JD.md lives; env-gated, None when unconfigured)."""
    if config.SINGAPORE_ROOT is None:
        return None
    return config.SINGAPORE_ROOT / "jobs" / "投递包"


def jds_dir() -> Path:
    """Landing directory for JDs fetched by the UI discovery layer (under gitignored data/)."""
    d = config.DATA_DIR / "jds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_path() -> Path:
    return config.DATA_DIR / "probe_queue.json"


# ---------------------------------------------------------------- queue read/write


def load_queue() -> list[dict[str, Any]]:
    p = queue_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(data.get("entries", []),
                  key=lambda e: str(e.get("deadline") or "9999-99-99"))


def save_queue(entries: list[dict[str, Any]]) -> None:
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


# ---------------------------------------------------------------- 1.1 per-job real analysis


def _agent_runs() -> list[dict[str, Any]]:
    p = config.DATA_DIR / "agent_runs.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _extraction_review() -> dict[str, Any]:
    p = config.DATA_DIR / "extraction_review.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_job_analysis(jd_id: str) -> dict[str, Any]:
    """Merge three real data sources to answer "what do we know about this job".

    A never-analyzed job is **analyzed=False** - the UI shows an honest empty
    state and never fabricates demo data.
    """
    entry = next((e for e in load_queue() if e.get("jd_id") == jd_id), {})
    runs = [r for r in _agent_runs() if r.get("jd_id") == jd_id]
    review = _extraction_review().get(jd_id)

    similar = None
    decision = None
    run_deadline = None
    if runs:
        last = runs[-1]
        st = last.get("similar_top")
        similar = st if isinstance(st, list) else ([st] if st else None)
        run_deadline = last.get("deadline")
        if last.get("action"):
            decision = {
                "action": last.get("action"),
                "by": last.get("decided_by", "human"),
                "reason": last.get("reason", ""),
                "at": last.get("decided_at", ""),
            }

    needs_human = None
    if isinstance(review, str):
        needs_human = review
    elif isinstance(review, dict):
        needs_human = review.get("reason") or json.dumps(review, ensure_ascii=False)[:200]

    deadline = entry.get("deadline") or run_deadline
    pkg = find_package(jd_id)
    return {
        "jd_id": jd_id,
        "channel": entry.get("channel", "unknown"),
        "queue_state": entry.get("state", ""),
        "deadline": deadline,
        "deadline_source": ("queue" if entry.get("deadline")
                            else ("agent run" if run_deadline else None)),
        "similar_top": similar,
        "decision": decision,
        "needs_human": needs_human,
        "analyzed": bool(runs) or bool(review),
        "jd_file": str(find_jd_file(jd_id)) if find_jd_file(jd_id) else None,
        "package": str(pkg) if pkg else None,
        "our_material": str(our_material_file(jd_id)) if our_material_file(jd_id) else None,
        "submitted_signal": package_submitted_signal(jd_id),
        "submission": entry.get("submission") or {},
    }


def find_jd_file(jd_id: str) -> Optional[Path]:
    """Locate JD.md: UI fetch directory -> real submission packages (env-gated). None when not found."""
    direct = jds_dir() / f"{jd_id}.md"
    if direct.exists():
        return direct
    pkg = find_package(jd_id)
    if pkg:
        f = pkg / "JD.md"
        if f.is_file():
            return f
    return None


_TITLE_CACHE: dict[str, str] = {}


def job_title(jd_id: str, max_len: int = 68) -> str:
    """Human-readable title from the JD's first ``#`` heading (fallback: the id).

    The queue only stores ``jd_id``; without a title the list is a wall of
    codes (``GOV-SYNTH00001`` means nothing to a human). Cached per process —
    a JD's heading does not change under it.
    """
    jid = str(jd_id or "").strip()
    if not jid:
        return ""
    if jid in _TITLE_CACHE:
        return _TITLE_CACHE[jid]
    title = jid
    f = find_jd_file(jid)
    if f:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError:
            pass
    if len(title) > max_len:
        title = title[: max_len - 1].rstrip() + "…"
    _TITLE_CACHE[jid] = title
    return title


def find_package(jd_id: str) -> Optional[Path]:
    """Locate the real submission-package directory (contains our materials and the submitted marker; env-gated)."""
    pkgs = packages_dir()
    if not (pkgs and pkgs.is_dir()):
        return None
    for d in sorted(pkgs.glob(f"*{jd_id}*")):
        if (d / "JD.md").is_file():
            return d
    org = re.split(r"[-_]", jd_id)[0]
    if org and len(org) >= 2:
        for d in sorted(pkgs.glob(f"*{org}*")):
            if (d / "JD.md").is_file():
                return d
    return None


def our_material_file(jd_id: str) -> Optional[Path]:
    """Our materials (the real object of phase-8 compliance): the submission package's CoverLetter.md."""
    pkg = find_package(jd_id)
    if pkg:
        f = pkg / "CoverLetter.md"
        if f.is_file():
            return f
    return None


def package_submitted_signal(jd_id: str) -> bool:
    """The "submitted" marker directory inside the package = the user's existing write-back convention (read by the tracking layer)."""
    pkg = find_package(jd_id)
    return bool(pkg and (pkg / "已投").exists())


# ---------------------------------------------------------------- 1.2 compliance on the fly


def run_compliance(jd_path: str | Path | None) -> dict[str, Any]:
    """Run the compliance engine on the selected JD.md (deterministic, zero LLM).

    Returns findings anchored to lines; when the JD file is missing,
    **jd_missing=True** and the UI says so plainly.
    """
    from .compliance.engine import check_files
    from .compliance.rules import get_rules

    if not jd_path or not Path(jd_path).is_file():
        return {"ok": False, "jd_missing": True, "findings": [], "n_rules": 0}
    try:
        rules = get_rules("all")
        report = check_files([jd_path], rules)
        fr = report.files[0]
        findings = [{
            "code": f.code, "desc": f.desc, "line": f.line,
            "segment": f.segment[:120], "hard": f.hard,
        } for f in fr.findings]
        return {"ok": fr.hard_count == 0, "jd_missing": False,
                "findings": findings, "n_rules": len(rules),
                "hard_count": fr.hard_count}
    except Exception as exc:                       # noqa: BLE001 - UI must not crash on engine errors
        return {"ok": False, "jd_missing": False, "error": str(exc),
                "findings": [], "n_rules": 0}


# ---------------------------------------------------------------- 1.3 material packing


def experience_source_text(profile: Any) -> dict[str, str]:
    """The original bullet text of each experience (the packing source) - **only
    confirmed / human-edited claims are collected** (unconfirmed facts must not
    flow into downstream materials).

    Returns {experience label: bullet original text}; labels look like
    "Zephyr Central Research Institute · 2023-2026".
    """
    from .profile.merge import materialize_experiences

    materialize_experiences(profile)
    out: dict[str, str] = {}
    for exp in profile.experiences:
        bullets = [c.text for c in exp.bullets if c.text.strip()]
        if bullets:
            span = f"{exp.start or '?'}–{exp.end or 'now'}"
            out[f"{exp.org or 'untitled'} · {span}"] = "\n".join(bullets)
    return out


def draft_materials(jd_path: str | Path | None,
                    source_text: str,
                    limits: dict[str, int | None]) -> dict[str, Any]:
    """Per-form packing: no-new-claims (delete-only), zero LLM.

    limits: {field_name: character cap or None (None = unverified, no trimming)}
    source_text: the original text from the user profile (every word of the candidate must come from it).
    """
    if not source_text.strip():
        return {"ok": False, "error": "no source text", "fields": []}
    fields = []
    for name, lim in limits.items():
        fl = FieldLimit(field_name=name, limit=lim,
                        source=("measured" if lim else "unverified"))
        cand = fit_to_budget(source_text, fl)
        rev = review_final(cand.text, fl)
        fields.append({
            "field": name, "limit": lim, "used": cand.used,
            "within_budget": cand.within_budget,
            "text": cand.text,
            "dropped": cand.dropped_sentences,
            "review_passed": rev.passed,
            "review_findings": [s for _, s in rev.findings],
        })
    return {"ok": all(f["review_passed"] for f in fields), "fields": fields}


CL_OPENING = "[write your opening yourself - I will not invent a greeting]"
CL_CLOSING = "[write your closing yourself]"


def draft_package(jd_path: str | Path | None,
                  source_map: dict[str, str],
                  limit: int | None) -> dict[str, Any]:
    """Resume + cover-letter drafts for one job (no-new-claims, zero LLM).

    Every sentence comes from ``source_map`` (the user's **confirmed**
    experience bullets) — packing only deletes, it never adds. The cover letter
    leaves its opening and closing as explicit placeholders: inventing a
    greeting ("Dear Hiring Manager, I am passionate about...") would be a new
    claim, which is exactly what the rules forbid, so the human writes those
    two lines.

    Returns ``{"ok", "resume", "cover_letter", "main_role", "fields"}``.
    """
    blocks: list[str] = []
    fields: list[dict[str, Any]] = []
    for label, src in source_map.items():
        if not (src or "").strip():
            continue
        res = draft_materials(jd_path, src, {"experience segment": limit})
        for f in res["fields"]:
            if f["text"].strip():
                blocks.append(f"{label}\n{f['text']}")
            fields.append(f)
    resume = "\n\n".join(blocks)

    main_role = next((k for k, v in source_map.items() if (v or "").strip()), "")
    body = ""
    if main_role:
        r = draft_materials(jd_path, source_map[main_role],
                            {"cover letter body": None})
        body = r["fields"][0]["text"] if r["fields"] else ""
    cover_letter = ""
    if body.strip():
        cover_letter = f"{CL_OPENING}\n\n{body}\n\n{CL_CLOSING}"
    return {"ok": bool(resume.strip()), "resume": resume,
            "cover_letter": cover_letter, "main_role": main_role,
            "fields": fields}


# ---------------------------------------------------------------- 2.2 preferences


DEFAULT_PREFERENCES: dict[str, Any] = {
    "channels": {},               # {channel_id: False=off}; absent = all on
    "exclude": [],                # institution code / name substrings (a jd_id hit means exclude)
    "roles": [],                  # soft preference: role-direction keywords (display only)
    "salary_policy": "ask",       # ask | range | none
}


def load_preferences() -> dict[str, Any]:
    p = config.DATA_DIR / "preferences.json"
    prefs = dict(DEFAULT_PREFERENCES)
    if p.exists():
        try:
            prefs.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return prefs


def save_preferences(prefs: dict[str, Any]) -> None:
    p = config.DATA_DIR / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_queue(entries: list[dict[str, Any]],
                 prefs: dict[str, Any]) -> tuple[list[dict[str, Any]],
                                                 list[tuple[dict[str, Any], str]]]:
    """Hard filter (ai-job-search semantics: exclusions get a listed reason, traceable)."""
    kept, excluded = [], []
    off = {k for k, v in (prefs.get("channels") or {}).items() if v is False}
    ex = [s.lower() for s in (prefs.get("exclude") or []) if s.strip()]
    for e in entries:
        jd_id = str(e.get("jd_id", ""))
        ch = str(e.get("channel", ""))
        if ch in off:
            excluded.append((e, f"channel {ch} is off"))
            continue
        hit = next((s for s in ex if s in jd_id.lower()), None)
        if hit:
            excluded.append((e, f"excluded: {hit}"))
            continue
        kept.append(e)
    return kept, excluded


# ------------------------------------------------- 1.6 reject/flag badges (phase 3)
# ai-job-search semantics: FAIL = exclude with a listed reason (already done by
# filter_queue); FLAG = keep with a status marker, not a rejection; deadline <7 days
# wins the tie-break; >30 days is only marked, **never rejected**. Exclusion reasons
# are traceable = the excluded list from filter_queue is derived live from
# preferences, with no separate storage (honest: reasons come from preferences,
# not a persisted score).

URGENT_DAYS = 7    # deadline <7 days -> urgent
FAR_DAYS = 30      # >30 days -> warn (mark only, never reject)
_HEALTHY = {"healthy", "ok", ""}


def days_left(deadline: Any, today: str | None = None) -> int | None:
    """Days from deadline to today (None = no deadline / unparseable). today is injectable (test determinism)."""
    d = str(deadline or "").strip()
    if not d:
        return None
    try:
        from datetime import date

        base = date.fromisoformat(str(today) if today else date.today().isoformat())
        return (date.fromisoformat(d) - base).days
    except ValueError:
        return None


def row_badges(row: dict[str, Any], states: dict[str, str] | None = None,
               today: str | None = None) -> list[tuple[str, str]]:
    """Soft-mark the **kept rows**. Returns [(text, level)], level in {urgent,warn,info}.

    No emoji by design (docs/22 §5.4): status is shown with colour + text only.
    The caller renders ``text`` in a colour keyed by ``level``.

    Badge sources (real fields on every row): deadline / channel_health / state;
    the states parameter is the channel-level F10 status fallback (queried when
    channel_health is absent).
    """
    states = states or {}
    out: list[tuple[str, str]] = []
    n = days_left(row.get("deadline"), today)
    if n is not None:
        if n < 0:
            out.append(("past deadline", "warn"))
        elif n == 0:
            out.append(("closes today", "urgent"))
        elif n <= URGENT_DAYS:
            out.append((f"closes in {n}d", "urgent"))
        elif n > FAR_DAYS:
            out.append((f"{n}d out — far, kept", "warn"))
    health = str(row.get("channel_health") or
                 states.get(str(row.get("channel", "")), ""))
    if health and health.lower() not in _HEALTHY:
        out.append((f"{row.get('channel', '?')}: {health}", "warn"))
    if str(row.get("state", "")).lower() == "draft":
        out.append(("draft — clock runs, no chasing", "info"))
    return out


def sort_queue(rows: list[dict[str, Any]], states: dict[str, str] | None = None,
               today: str | None = None) -> list[dict[str, Any]]:
    """Urgency sort: urgent (deadline <=7d) wins the tie-break; then deadline
    ascending; no deadline sinks to the bottom; warn ranks after clean rows."""

    def key(r: dict[str, Any]):
        badges = row_badges(r, states, today)
        urgent = any(lvl == "urgent" for _, lvl in badges)
        warn = any(lvl == "warn" for _, lvl in badges)
        n = days_left(r.get("deadline"), today)
        return (0 if urgent else 1,
                n if n is not None else 10 ** 6,
                0 if warn else 1)

    return sorted(rows, key=key)


# ------------------------------------------------ 3.2 keyword coverage four-state table (phase 3)
# ApplyPilot pattern: covered / synonym-only / missing-have / missing-gap.
# **Deterministic heuristic, zero LLM** (design constraint: the local LLM is
# off the table; 1.5B list extraction is the measured D9 boundary) - the UI
# must label it "heuristic, verify yourself".
# Synonym groups can be extended via data/keyword_synonyms.json (gitignored, user-editable).

DEFAULT_SYNONYM_GROUPS: list[set[str]] = [
    {"ai", "artificial intelligence", "machine learning", "ml"},
    {"product management", "product owner", "product lead"},
    {"stakeholder management", "stakeholder engagement", "stakeholder"},
    {"ci/cd", "continuous integration", "continuous delivery", "devops"},
    {"kubernetes", "k8s", "container orchestration"},
    {"python", "python3"},
    {"agile", "scrum"},
    {"roadmap", "product roadmap"},
    {"data governance", "data management", "data quality"},
    {"cybersecurity", "security engineering", "infosec"},
]

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9/+#.-]*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,8}")
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "you", "your",
    "our", "are", "will", "have", "has", "not", "but", "all", "can", "who",
    "how", "what", "when", "where", "which", "their", "them", "they", "its",
    "into", "over", "under", "than", "then", "also", "such", "using", "use",
    "able", "must", "should", "would", "may", "might", "shall", "been",
    "being", "was", "were", "did", "does", "done", "each", "more", "most",
    "other", "some", "very", "just", "like", "well", "good", "new", "old",
}
# Ledger template labels + function words (the real JD.md is in Chinese-ledger
# form; without filtering these, candidates are all template noise like
# "deadline/page not published")
_CJK_STOP = {
    "截止", "日期", "页面", "未公布", "通道", "记录", "机构", "岗位",
    "雇佣", "类型", "原始", "链接", "评分", "档位", "裁决", "门槛",
    "判定", "明示", "逐条", "确认", "开放", "人工", "全职", "兼职",
    "以及", "与", "和", "或", "在", "是", "有", "并", "等", "对", "为",
    "以", "及", "该", "其", "此", "可以", "进行", "相关", "如下",
    "以上", "以下", "通过", "根据", "包括", "具有", "具备", "负责",
    "可投", "不投", "备选", "待核", "已关闭", "已投递", "本目录",
}


def _norm(s: str) -> str:
    return " ".join(_WORD_RE.findall(str(s).lower()))


def _content_words(kw: str) -> list[str]:
    return [w for w in _WORD_RE.findall(kw.lower())
            if w not in _STOP and len(w) > 2]


def _looks_like_path_or_markup(c: str) -> bool:
    """Filter out any path/code/markdown mixed into candidates (measured: real JDs
    contain backticked content like docs/jd_registry_ops.py). Single-slash terms
    like CI/CD are kept."""
    if c.endswith((".py", ".md", ".json", ".txt", ".db")):
        return True
    if c.lower().startswith(("http", "www.")):
        return True
    if "\\" in c or "*" in c or "#" in c or "|" in c:
        return True
    return c.count("/") > 1


def _synonym_groups() -> list[set[str]]:
    import json

    p = config.DATA_DIR / "keyword_synonyms.json"
    groups = [set(g) for g in DEFAULT_SYNONYM_GROUPS]
    if p.exists():
        try:
            extra = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(extra, list):
                groups += [set(str(x).lower() for x in g) for g in extra
                           if isinstance(g, list)]
        except (OSError, json.JSONDecodeError):
            pass
    return groups


def jd_keywords(jd_text: str, limit: int = 15) -> list[str]:
    """JD keyword candidates (heuristic, zero LLM): English = quoted short phrases /
    backticks / capitalized sequences / all-caps tokens; Chinese = 2-8 char runs
    (ledger template words filtered out). Frequency first.

    **Display aid, not extracted fact** - callers' UI must label it honestly
    (heuristic). The real JD.md is in Chinese-ledger form (English requirements
    hide inside 『』), hence the bilingual parallel path.
    """
    # Content is content: English, Chinese, Malay — whatever the JD actually
    # says is reported as-is. Never "clean" a language away.
    cands: list[str] = []
    cands += re.findall(r"[『「]([^』」\n]{3,60})[』」]", jd_text)
    cands += re.findall(r'"([^"\n]{3,60})"', jd_text)
    cands += re.findall(r"`([^`\n]{2,40})`", jd_text)
    cands += re.findall(r"\b([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,2})\b", jd_text)
    cands += re.findall(r"\b([A-Z]{2,6})\b", jd_text)
    cands += re.findall(r"\b([A-Z]{2,4}(?:/[A-Z]{2,4})+)\b", jd_text)
    freq: dict[str, int] = {}
    for c in cands:
        c = c.strip(" .,;:·-—–")
        if not c or len(c) > 48 or _looks_like_path_or_markup(c):
            continue
        if not _content_words(c):
            continue                      # pure stop words / too short
        if len(c.split()) > 5:
            continue                      # long sentences are clauses, not keywords
        freq[c] = freq.get(c, 0) + 1
    # Chinese words: extracted only from **bounded structural positions**
    # (『』 quotes / （） parentheses / **bold**) - Chinese prose is not
    # scanned: run extraction without a tokenizer produces sentence fragments
    # (measured: "字化转型咨询经验"-type noise), which violates the quality
    # bar. Only terms in structural positions count. Public-product user data
    # is mostly non-Chinese (English / Malay ...); this path is compatibility only.
    cjk_freq: dict[str, int] = {}
    bounded: list[str] = []
    bounded += re.findall(r"『([^』\n]{2,80})』", jd_text)
    bounded += re.findall(r"（([^（）\n]{2,60})）", jd_text)
    bounded += re.findall(r"\*\*([^*\n]{2,60})\*\*", jd_text)
    for span in bounded:
        for run in _CJK_RUN_RE.findall(span):
            if run in _CJK_STOP:
                continue
            cjk_freq[run] = cjk_freq.get(run, 0) + 1
    en_ranked = sorted(freq.items(),
                       key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    cjk_ranked = sorted(cjk_freq.items(),
                        key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    half = max(1, limit // 2)
    out = [k for k, _ in en_ranked[:limit - half]]
    out += [k for k, _ in cjk_ranked[:half]]
    return out[:limit]


def funnel_stats() -> dict[str, int]:
    """Submission funnel (/audit phase 3): queued -> analyzed -> material -> decided ->
    submitted -> engaged. All figures come from real data sources; stages with
    nothing are honestly 0 - no fabricated middle of the funnel (empty-state discipline)."""
    entries = load_queue()
    stats = {"queued": len(entries), "analyzed": 0, "material": 0,
             "decided": 0, "submitted": 0, "engaged": 0}
    for e in entries:
        jd_id = str(e.get("jd_id", ""))
        a = load_job_analysis(jd_id)
        if a.get("analyzed"):
            stats["analyzed"] += 1
        if a.get("our_material"):
            stats["material"] += 1
        if a.get("decision"):
            stats["decided"] += 1
        sub = e.get("submission") or {}
        if (sub.get("status") == "submitted"
                or str(e.get("state", "")).lower() in ("submitted", "done")):
            stats["submitted"] += 1
        if sub.get("followups"):
            stats["engaged"] += 1
    return stats


FUNNEL_STAGES = ["queued", "analyzed", "material", "decided",
                 "submitted", "engaged"]


def pack_diff(source_text: str, packed_text: str) -> list[tuple[str, str]]:
    """Word-level diff (ai-job-search structured-diff paradigm, zero LLM).

    Returns [(op, text)], op in {equal,delete,insert}. Packing only
    deletes/reorders, never adds, so insert should only come from
    recombinations of original words - the UI uses it to let people review
    "what was trimmed out".
    """
    import difflib

    a, b = str(source_text).split(), str(packed_text).split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.append(("equal", " ".join(a[i1:i2])))
        elif op == "delete":
            out.append(("delete", " ".join(a[i1:i2])))
        elif op == "insert":
            out.append(("insert", " ".join(b[j1:j2])))
        else:                       # replace: delete old + insert new (new words still come from the original)
            out.append(("delete", " ".join(a[i1:i2])))
            out.append(("insert", " ".join(b[j1:j2])))
    return out


def keyword_coverage(jd_text: str, material_text: str,
                     limit: int = 15) -> dict[str, list[str]]:
    """Four-state coverage: covered / synonym_only / missing_have / missing_gap.

    Adjudication order: literal (English normalized containment; Chinese direct
    substring) -> synonym-group member -> content-word / 2-char substring
    partial overlap (missing-have) -> nothing at all (missing-gap).
    Deterministic and reproducible; synonym groups are user-extensible via
    data/keyword_synonyms.json.
    """
    mat_raw = str(material_text)
    mat = _norm(mat_raw)
    groups = _synonym_groups()
    out: dict[str, list[str]] = {"covered": [], "synonym_only": [],
                                 "missing_have": [], "missing_gap": []}
    for kw in jd_keywords(jd_text, limit=limit):
        if _CJK_RUN_RE.fullmatch(kw):
            if kw in mat_raw:
                out["covered"].append(kw)
                continue
            subs = {kw[i:i + 2] for i in range(len(kw) - 1)}
            if any(s in mat_raw for s in subs if s not in _CJK_STOP):
                out["missing_have"].append(kw)
                continue
            out["missing_gap"].append(kw)
            continue
        n = _norm(kw)
        if n and n in mat:
            out["covered"].append(kw)
            continue
        syn = False
        for g in groups:
            if n in g or kw.lower() in g:
                if any(_norm(m) in mat for m in g
                       if _norm(m) and _norm(m) != n):
                    syn = True
                    break
        if syn:
            out["synonym_only"].append(kw)
            continue
        cw = _content_words(kw)
        if cw and any(w in mat for w in cw):
            out["missing_have"].append(kw)
            continue
        out["missing_gap"].append(kw)
    return out


# ---------------------------------------------------------------- 2.1 settings/health


def load_user_settings() -> dict[str, Any]:
    """data/settings.json (gitignored). env takes precedence - this file is only the default."""
    p = config.DATA_DIR / "settings.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_user_settings(d: dict[str, Any]) -> None:
    p = config.DATA_DIR / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_HEALTH_TTL_S = 20.0


def settings_health() -> dict[str, Any]:
    """System health check (ApplyPilot doctor mode): per-item OK/WARN/optional + what is missing, with the fix.

    TTL cache: probing has a cost (even local loopback is millisecond-level), so
    repeated renders of the top bar / settings page do not re-probe.
    """
    import time as _time

    now = _time.time()
    if (_HEALTH_CACHE["data"] is not None
            and now - _HEALTH_CACHE["ts"] < _HEALTH_TTL_S):
        return _HEALTH_CACHE["data"]

    from .llm.client import (DEFAULT_WORKBUDDY_BASE_URL, load_settings,
                             probe_endpoint)

    # Probe memoization: endpoints this function has probed itself; load_settings uses the results directly (no re-probing)
    _probe_cache: dict[str, bool] = {}

    def _memo_probe(url: str, timeout: float = 0.75) -> bool:
        if url not in _probe_cache:
            _probe_cache[url] = probe_endpoint(url, timeout)
        return _probe_cache[url]

    items: list[dict[str, Any]] = []

    # LLM backend four-level chain (workbuddy -> cloud -> local; auto probing same as client.py)
    wb = _memo_probe(DEFAULT_WORKBUDDY_BASE_URL)
    items.append({"name": "WorkBuddy LLM proxy (8787)",
                  "status": "ok" if wb else "off",
                  "hint": "" if wb else "start workbuddy-connect-api, or pick another backend"})
    items.append({"name": "Cloud backend (opt-in)",
                  "status": "opt-in",
                  "hint": "APA_LLM_BACKEND=cloud + APA_ALLOW_CLOUD_LLM=1 + endpoint/key"})
    loc = _memo_probe("http://127.0.0.1:8080")
    items.append({"name": "Local llama.cpp (8080)",
                  "status": "ok" if loc else "off",
                  "hint": "" if loc else "1.5B too weak for list extraction (D9) — start a stronger model or use another backend"})

    # Browser channel (apa fetch / discovery dependency)
    from .f10.browser import DEFAULT_CHROME, _workspace
    chrome_ok = Path(DEFAULT_CHROME).exists()
    items.append({"name": "Chrome (render channel)",
                  "status": "ok" if chrome_ok else "off",
                  "hint": "" if chrome_ok else "set APA_CHROME_PATH — needed for SPA pages & discovery"})
    pw_ok = (_workspace() / "node_modules" / "playwright-core").exists()
    items.append({"name": "playwright-core (node workspace)",
                  "status": "ok" if pw_ok else "off",
                  "hint": "" if pw_ok else "npm i playwright-core in the node workspace"})

    # Job-application workspace (where the JD packages live)
    sg_ok = config.SINGAPORE_ROOT is not None and packages_dir().is_dir()
    items.append({"name": "Job-application workspace (APA_SINGAPORE_ROOT)",
                  "status": "ok" if sg_ok else "off",
                  "hint": "" if sg_ok else "set APA_SINGAPORE_ROOT — real JD packages live there"})

    # Data-sovereignty label (sovereignty three-state, follows the actual backend; settings already probed,
    # injected to avoid repeated probing)
    from .sovereignty import current_status
    try:
        from .llm.client import load_settings
        s = load_settings(probe=_memo_probe)
        backend = s.backend
    except Exception:                               # noqa: BLE001 - health check proceeds even with no backend
        s = None
        backend = "unknown"
    sov = current_status(s).to_dict()

    result = {"items": items, "backend": backend, "sovereignty": sov,
              "settings": load_user_settings()}
    _HEALTH_CACHE.update({"ts": now, "data": result})
    return result


# ---------------------------------------------------------------- 2.3 discovery: URL -> JD


def add_jd_by_url(url: str, wait_ms: int = 8000) -> dict[str, Any]:
    """Paste a URL -> browser rendered fetch -> land JD.md -> enter the queue.

    Discipline: on fetch failure, **return blocked/error honestly** - never
    queue silently (the D4 lesson's UI-side expression); every stored JD
    carries a source URL header (traceable - all of APA's value rests on
    being able to trace back).
    """
    from .f10.browser import BrowserFetch

    url = url.strip()
    if not re.match(r"^https?://", url):
        return {"ok": False, "error": "not an http(s) URL", "blocked": True}
    res = BrowserFetch(wait_ms=wait_ms).fetch(url)
    if res.error:
        return {"ok": False, "error": res.error, "blocked": True}
    if not res.rendered_ok:
        return {"ok": False,
                "error": f"page rendered too little ({len(res.text)} chars) — "
                         "SPA may need a longer wait",
                "blocked": True}
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", res.title or url)[:60].strip("-") or "jd"
    jd_id = slug.upper()
    path = jds_dir() / f"{jd_id}.md"
    header = (f"<!-- source: {url} -->\n"
              f"<!-- fetched: {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
              f"via render channel -->\n\n# {res.title}\n\n")
    path.write_text(header + res.text, encoding="utf-8")

    entries = load_queue()
    if not any(e.get("jd_id") == jd_id for e in entries):
        entries.append({"jd_id": jd_id, "channel": "fetched",
                        "state": "draft", "deadline": "",
                        "source_url": url})
        save_queue(entries)
    return {"ok": True, "jd_id": jd_id, "title": res.title,
            "path": str(path), "chars": len(res.text)}


# ---------------------------------------------------------------- 2.3b discovery: query -> search page -> JD links

#: Stage-4 channels. The browser **render** channel (f10/browser.BrowserFetch)
#: is what makes search possible where headless SSR/API scraping is blocked
#: (see docs/21 stage 4 and f10/browser.py) — there is no anti-scrape issue on
#: the search-results page itself when a real browser opens it.
#:
#: ``search_url`` is best-effort; the gov portal URL drifts, so the Discover UI
#: also lets the user **paste a real search-results URL** (docs/21's documented
#: fallback path). ``job_link`` / ``job_id`` are pure regexes so extraction is
#: unit-testable with a saved HTML fixture (no network).
#:
#: **Empirical note (live MyCareersFuture, 2026-10-20):** result anchors are
#: *relative* paths (``/job/<category>/<slug>-<32hex>?source=MCF&event=Search``)
#: with HTML-escaped ``&amp;`` — so extraction resolves relative hrefs against
#: ``base`` and unescapes entities before matching.
SEARCH_CHANNELS: dict[str, dict[str, Any]] = {
    "mycareersfuture": {
        "label": "MyCareersFuture",
        "base": "https://www.mycareersfuture.gov.sg",
        "search_url": "https://www.mycareersfuture.gov.sg/search?search={query}",
        "job_link": re.compile(
            r"(?:https?://www\.mycareersfuture\.gov\.sg)?/job/[^\"'<\s]+", re.I),
        # MyCareersFuture ids are 32-hex UUIDs at the end of the slug.
        "job_id": re.compile(r"([0-9a-f]{32})(?:[?\"'<\s]|$)"),
    },
    "careersgov": {
        "label": "Careers@Gov",
        "base": "https://www.careers.gov.sg",
        # Best-effort template; if it drifts, paste the real search URL instead.
        "search_url": "https://www.careers.gov.sg/jobs?search={query}",
        "job_link": re.compile(
            r"(?:https?://(?:www\.|jobs\.)?careers\.gov\.sg)?/(?:job|jobs)/[^\"'<\s]+",
            re.I),
        "job_id": re.compile(r"/(?:job|jobs)/([A-Za-z0-9\-_]+)"),
    },
}


def _extract_jd_links(html: str, channel: str, origin: str = "") -> list[dict[str, str]]:
    """Pull JD detail links out of a rendered search-results page (pure, testable).

    Returns a de-duplicated list of ``{"title", "url", "job_id"}``. Relative
    hrefs are resolved against ``origin`` (search pages emit ``/job/...`` paths);
    HTML entities in hrefs are unescaped (``&amp;`` -> ``&``). ``title`` falls
    back to the URL when the anchor carries no text; ``job_id`` is the normalized
    per-channel id used for dedup (URL + normalized id, per docs/21).
    """
    spec = SEARCH_CHANNELS.get(channel)
    if spec is None:
        return []
    pat = spec["job_link"]
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for m in re.finditer(
            r"<a\b[^>]*\bhref=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.I | re.S):
        raw = m.group(1).strip()
        url = _html.unescape(raw)
        if not pat.search(url):
            continue
        if not re.match(r"^https?://", url):
            if origin and url.startswith("/"):
                url = origin.rstrip("/") + url
            else:
                continue
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        title = text or url
        jid = ""
        jm = spec["job_id"].search(url)
        if jm:
            jid = jm.group(1)
        key = (jid or url).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "url": url, "job_id": jid})
    return out


def search_jds(channel: str, query: str = "", search_url: str = "",
               wait_ms: int = 8000) -> dict[str, Any]:
    """Stage-4 conditional search: render a search page and extract JD links.

    Two entry paths (docs/21 stage 4):
      - ``query`` given  -> build the channel's search URL and render it
      - ``search_url`` given (user pasted a results page) -> render that directly

    Discipline: on any failure, return ``blocked``/``error`` honestly - never
    silently pretend the channel returned nothing.
    """
    from .f10.browser import BrowserFetch

    if channel not in SEARCH_CHANNELS:
        return {"ok": False, "error": f"unknown channel: {channel}", "blocked": True}
    spec = SEARCH_CHANNELS[channel]
    if search_url:
        target = search_url.strip()
    elif query:
        q = query.strip().replace(" ", "+")
        target = spec["search_url"].format(query=q)
    else:
        return {"ok": False, "error": "provide a query or a search URL",
                "blocked": True}
    if not re.match(r"^https?://", target):
        return {"ok": False, "error": "not an http(s) URL", "blocked": True}

    res = BrowserFetch(wait_ms=wait_ms).fetch(target)
    if res.error:
        return {"ok": False, "error": res.error, "blocked": True, "search_url": target}
    if not res.rendered_ok:
        return {"ok": False,
                "error": f"search page rendered too little ({len(res.text)} chars) "
                         f"- SPA may need a longer wait",
                "blocked": True, "search_url": target}
    items = _extract_jd_links(res.html, channel, spec["base"])
    return {"ok": True, "channel": channel, "search_url": target,
            "items": items, "count": len(items)}


# ---------------------------------------------------------------- 2.4 tracking (F12-min)


FOLLOWUP_AFTER_DAYS = 10      # single source: 10d / 2 times / 60d
FOLLOWUP_MAX = 2
STALE_AFTER_DAYS = 60


def mark_submitted(jd_id: str) -> dict[str, Any]:
    """User write-back "submitted" - **the sole authority** (three-tier grading).

    The drafted deadline clock keeps running; the submission diary records the
    real submission date (ai-job-search lesson: when state leaves drafted the
    date must be rewritten - never leave the draft date).
    """
    entries = load_queue()
    for e in entries:
        if e.get("jd_id") == jd_id:
            sub = e.get("submission") or {}
            sub.update({"status": "submitted",
                        "submitted_at": date.today().isoformat(),
                        "followups": sub.get("followups", [])})
            e["submission"] = sub
            e["state"] = "submitted"
            save_queue(entries)
            return {"ok": True, "jd_id": jd_id}
    return {"ok": False, "error": f"{jd_id} not in queue"}


def record_followup(jd_id: str) -> dict[str, Any]:
    entries = load_queue()
    for e in entries:
        if e.get("jd_id") == jd_id:
            sub = e.get("submission") or {}
            fus = sub.get("followups", [])
            if len(fus) >= FOLLOWUP_MAX:
                return {"ok": False,
                        "error": f"already followed up {FOLLOWUP_MAX} times — "
                                 "log no_response instead of chasing forever"}
            fus.append(date.today().isoformat())
            sub["followups"] = fus
            e["submission"] = sub
            save_queue(entries)
            return {"ok": True, "followups": fus}
    return {"ok": False, "error": f"{jd_id} not in queue"}


def followup_hint(entry: dict[str, Any]) -> Optional[str]:
    """Follow-up hint (suggestion-level - **never sends anything automatically**, tracking discipline)."""
    sub = entry.get("submission") or {}
    if sub.get("status") != "submitted":
        return None
    try:
        submitted = date.fromisoformat(str(sub.get("submitted_at")))
    except ValueError:
        return None
    age = (date.today() - submitted).days
    n = len(sub.get("followups", []))
    if age >= STALE_AFTER_DAYS:
        return f"no response for {age}d — suggest closing as no_response (your call)"
    if age >= FOLLOWUP_AFTER_DAYS and n < FOLLOWUP_MAX:
        return f"submitted {age}d ago · follow-up {n}/{FOLLOWUP_MAX} available"
    if n >= FOLLOWUP_MAX:
        return f"followed up {n}x — waiting is the right move now"
    return None
