"""uiview data assembly layer tests (the first/second-phase foundation).

Discipline: zero network (probe/fetch all monkeypatched), zero LLM, pure
functions; all data lands in tmp (monkeypatch config.DATA_DIR) — never touches
the real data/.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa import config, uiview


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    return d


# ---------------------------------------------------------------- queue and filtering


class TestQueueIO:
    def test_roundtrip_sorted_by_deadline(self, tmp_data):
        uiview.save_queue([{"jd_id": "B", "deadline": "2026-11-01"},
                           {"jd_id": "A", "deadline": "2026-10-20"},
                           {"jd_id": "C", "deadline": ""}])
        assert [e["jd_id"] for e in uiview.load_queue()] == ["A", "B", "C"]

    def test_missing_queue_is_empty_not_crash(self, tmp_data):
        assert uiview.load_queue() == []


class TestFilterQueue:
    def test_channel_off_excluded_with_reason(self):
        entries = [{"jd_id": "X", "channel": "jobstreet"}]
        kept, excluded = uiview.filter_queue(
            entries, {"channels": {"jobstreet": False}})
        assert kept == []
        assert excluded[0][1] == "channel jobstreet is off"

    def test_exclude_matches_jd_id_case_insensitive(self):
        entries = [{"jd_id": "AGG-Aurora", "channel": "jobstreet"},
                   {"jd_id": "GOV-1", "channel": "hrp"}]
        kept, excluded = uiview.filter_queue(entries, {"exclude": ["aurora"]})
        assert [e["jd_id"] for e in kept] == ["GOV-1"]
        assert "excluded: aurora" in excluded[0][1]

    def test_empty_prefs_keep_all(self):
        entries = [{"jd_id": "A"}, {"jd_id": "B"}]
        kept, excluded = uiview.filter_queue(entries, uiview.DEFAULT_PREFERENCES)
        assert len(kept) == 2 and excluded == []


# ---------------------------------------------------------------- per-posting analysis


class TestJobAnalysis:
    def test_no_data_is_honestly_not_analyzed(self, tmp_data):
        uiview.save_queue([{"jd_id": "NOPE-1", "channel": "hrp"}])
        a = uiview.load_job_analysis("NOPE-1")
        assert a["analyzed"] is False
        assert a["similar_top"] is None and a["decision"] is None

    def test_agent_run_and_review_surface(self, tmp_data):
        uiview.save_queue([{"jd_id": "GOV-9", "channel": "hrp",
                            "deadline": "2026-10-22"}])
        (tmp_data / "agent_runs.jsonl").write_text(json.dumps({
            "jd_id": "GOV-9", "deadline": "2026-10-22",
            "similar_top": ["GOV-8", "GOV-7"], "action": "approve",
            "decided_by": "human", "reason": "strong fit"}) + "\n",
            encoding="utf-8")
        (tmp_data / "extraction_review.json").write_text(json.dumps(
            {"GOV-9": "requirements: expected JSON array"}), encoding="utf-8")
        a = uiview.load_job_analysis("GOV-9")
        assert a["analyzed"] is True
        assert a["similar_top"] == ["GOV-8", "GOV-7"]
        assert a["decision"]["action"] == "approve"
        assert "requirements" in a["needs_human"]
        assert a["deadline"] == "2026-10-22"


# ---------------------------------------------------------------- compliance live run


class TestCompliance:
    def test_missing_file_is_flagged_not_faked(self, tmp_data):
        r = uiview.run_compliance(None)
        assert r["jd_missing"] is True and r["findings"] == []

    def test_real_golden_file_produces_located_findings(self):
        """The golden accident-1 dirty version must be caught by a live run
        (the same path that wires the engine into the UI)."""
        dirty = Path("evals/golden-synthetic/a1_residue/"
                     "CoverLetter_Fabrikam_before.md")
        r = uiview.run_compliance(dirty)
        assert r["jd_missing"] is False
        assert r["n_rules"] > 0
        assert any(f["line"] > 0 for f in r["findings"])


# ---------------------------------------------------------------- material packing


class TestDraftMaterials:
    SOURCE = ("I led the platform team that cut inference cost by 42 percent. "
              "I shipped six model families to production last year. "
              "I wrote my thesis on alignment theory years ago.")

    def test_budget_packs_and_drops_transparently(self):
        r = uiview.draft_materials(None, self.SOURCE, {"experience": 100})
        f = r["fields"][0]
        assert f["limit"] == 100
        assert f["used"] <= 100 and f["within_budget"] is True
        assert f["review_passed"] is True
        assert f["dropped"], "source longer than budget must drop something"

    def test_no_new_claims_invariant(self):
        """Packing iron rule: the candidate word set ⊆ the source word set
        (no fabrication surface)."""
        r = uiview.draft_materials(None, self.SOURCE, {"experience": 120})
        src_words = set(self.SOURCE.lower().split())
        for f in r["fields"]:
            assert set(f["text"].lower().split()) <= src_words

    def test_unverified_limit_does_not_crop(self):
        r = uiview.draft_materials(None, self.SOURCE, {"experience": None})
        f = r["fields"][0]
        assert f["limit"] is None and f["dropped"] == []
        assert f["used"] == len(self.SOURCE)

    def test_empty_source_is_refused(self):
        r = uiview.draft_materials(None, "   ", {"experience": 256})
        assert r["ok"] is False


# ---------------------------------------------------------------- preference storage


class TestPreferences:
    def test_defaults_when_no_file(self, tmp_data):
        assert uiview.load_preferences()["salary_policy"] == "ask"

    def test_save_roundtrip(self, tmp_data):
        uiview.save_preferences({"exclude": ["aurora"], "salary_policy": "none"})
        p = uiview.load_preferences()
        assert p["exclude"] == ["aurora"] and p["salary_policy"] == "none"


# ---------------------------------------------------------------- settings health check


class TestSettingsHealth:
    def _fresh(self):
        uiview._HEALTH_CACHE.update({"ts": 0.0, "data": None})

    def test_probes_are_injected_not_networked(self, tmp_data, monkeypatch):
        self._fresh()
        monkeypatch.setattr("apa.llm.client.probe_endpoint",
                            lambda url, timeout=2.0: "8787" in url)
        h = uiview.settings_health()
        by = {i["name"]: i["status"] for i in h["items"]}
        assert by["WorkBuddy LLM proxy (8787)"] == "ok"
        assert by["Local llama.cpp (8080)"] == "off"

    def test_sovereignty_label_present(self, tmp_data, monkeypatch):
        self._fresh()
        monkeypatch.setattr("apa.llm.client.probe_endpoint",
                            lambda url, timeout=2.0: False)
        h = uiview.settings_health()
        assert h["sovereignty"]["label"]

    def test_cache_prevents_repeat_probing(self, tmp_data, monkeypatch):
        self._fresh()
        calls = []

        def counting_probe(url, timeout=2.0):
            calls.append(url)
            return False

        monkeypatch.setattr("apa.llm.client.probe_endpoint", counting_probe)
        uiview.settings_health()
        n = len(calls)
        uiview.settings_health()
        assert len(calls) == n, "second call must hit the TTL cache"


# ---------------------------------------------------------------- discovery


class TestAddJdByUrl:
    def test_non_http_rejected(self):
        r = uiview.add_jd_by_url("ftp://x")
        assert r["ok"] is False and r["blocked"] is True

    def test_render_failure_is_blocked_not_enqueued(self, tmp_data, monkeypatch):
        from apa.f10.browser import FetchResult

        class Dead:
            def __init__(self, **kw):
                pass

            def fetch(self, url):
                return FetchResult(url=url, error="chrome missing")

        monkeypatch.setattr("apa.f10.browser.BrowserFetch", Dead)
        r = uiview.add_jd_by_url("https://example.test/job")
        assert r["ok"] is False and r["blocked"] is True
        assert uiview.load_queue() == []          # failures are not enqueued (discipline)

    def test_success_saves_with_source_header_and_enqueues(self, tmp_data,
                                                           monkeypatch):
        from apa.f10.browser import FetchResult

        class Ok:
            def __init__(self, **kw):
                pass

            def fetch(self, url):
                return FetchResult(url=url, status=200, title="AI Engineer",
                                   text="x" * 3000)

        monkeypatch.setattr("apa.f10.browser.BrowserFetch", Ok)
        r = uiview.add_jd_by_url("https://example.test/job/1")
        assert r["ok"] is True
        saved = Path(r["path"]).read_text(encoding="utf-8")
        assert "source: https://example.test/job/1" in saved
        assert any(e["jd_id"] == r["jd_id"] for e in uiview.load_queue())


# ---------------------------------------------------------------- tracking (F12-min)


class TestTracking:
    def _queue_with(self, tmp_data):
        uiview.save_queue([{"jd_id": "GOV-1", "channel": "hrp",
                            "deadline": "2026-10-20"}])
        return uiview.load_queue()[0]

    def test_mark_submitted_writes_user_authority(self, tmp_data):
        self._queue_with(tmp_data)
        assert uiview.mark_submitted("GOV-1")["ok"] is True
        e = uiview.load_queue()[0]
        assert e["submission"]["status"] == "submitted"
        assert e["state"] == "submitted"

    def test_followup_capped_at_two(self, tmp_data, monkeypatch):
        self._queue_with(tmp_data)
        uiview.mark_submitted("GOV-1")
        assert uiview.record_followup("GOV-1")["ok"] is True
        assert uiview.record_followup("GOV-1")["ok"] is True
        third = uiview.record_followup("GOV-1")
        assert third["ok"] is False and "no_response" in third["error"]

    def test_drafted_never_gets_followup_hint(self, tmp_data):
        e = self._queue_with(tmp_data)
        assert uiview.followup_hint(e) is None

    def test_stale_hint_after_60d(self, tmp_data):
        self._queue_with(tmp_data)
        uiview.mark_submitted("GOV-1")
        entries = uiview.load_queue()
        entries[0]["submission"]["submitted_at"] = "2026-08-01"
        uiview.save_queue(entries)
        hint = uiview.followup_hint(uiview.load_queue()[0])
        assert "no response" in hint and "no_response" in hint


# ------------------------------------------------- rejection/marking badges (phase 3)


class TestBadges:
    TODAY = "2026-10-19"

    def test_days_left_none_for_open_or_garbage(self):
        assert uiview.days_left("") is None
        assert uiview.days_left(None) is None
        assert uiview.days_left("not-a-date") is None

    def test_days_left_injected_today(self):
        assert uiview.days_left("2026-10-22", today=self.TODAY) == 3
        assert uiview.days_left("2026-10-19", today=self.TODAY) == 0
        assert uiview.days_left("2026-10-01", today=self.TODAY) == -18

    def test_today_and_window_are_urgent_fire(self):
        b = uiview.row_badges({"deadline": self.TODAY}, today=self.TODAY)
        assert b == [("closes today", "urgent")]
        b = uiview.row_badges({"deadline": "2026-10-25"}, today=self.TODAY)
        assert b == [("closes in 6d", "urgent")]

    def test_far_out_warns_but_never_drops(self):
        b = uiview.row_badges({"deadline": "2026-12-30"}, today=self.TODAY)
        assert b == [("72d out — far, kept", "warn")]
        # >30d is only a marker: the filter layer never excludes because of
        # it (a lesson from the ai-job-search project)
        kept, excluded = uiview.filter_queue(
            [{"jd_id": "FAR-1", "deadline": "2026-12-30"}], {})
        assert kept and not excluded

    def test_past_deadline_warns(self):
        b = uiview.row_badges({"deadline": "2026-10-01"}, today=self.TODAY)
        assert b == [("past deadline", "warn")]

    def test_unhealthy_channel_warns(self):
        b = uiview.row_badges({"channel": "hrp", "channel_health": "degraded"},
                              today=self.TODAY)
        assert ("hrp: degraded", "warn") in b

    def test_channel_state_fallback_when_row_field_missing(self):
        b = uiview.row_badges({"channel": "hrp"}, states={"hrp": "down"},
                              today=self.TODAY)
        assert ("hrp: down", "warn") in b

    def test_draft_badge_is_info_not_warn(self):
        b = uiview.row_badges({"state": "draft"}, today=self.TODAY)
        assert b == [("draft — clock runs, no chasing", "info")]

    def test_real_queue_shape_fabrikam_first_double_badge(self):
        """Real 5-row anchor (2026-10-19): Fabrikam-Helios closes today + hrp
        degraded → double badge and sorted first; GOV +3d second; open-ended
        sinks to the bottom."""
        rows = [
            {"jd_id": "GOV-SYNTH00001", "channel": "workable",
             "channel_health": "healthy", "deadline": "2026-10-22",
             "state": "ready"},
            {"jd_id": "Contoso-27", "channel": "mycareersfuture",
             "channel_health": "healthy", "deadline": "2026-10-27",
             "state": "ready"},
            {"jd_id": "Fabrikam-Helios", "channel": "hrp",
             "channel_health": "degraded", "deadline": "2026-10-19",
             "state": "draft"},
            {"jd_id": "SIA-ADS", "channel": "greenhouse",
             "channel_health": "healthy", "deadline": "2026-11-05",
             "state": "ready"},
            {"jd_id": "ACME-AIDO", "channel": "workday",
             "channel_health": "healthy", "deadline": "", "state": "draft"},
        ]
        order = [r["jd_id"] for r in uiview.sort_queue(rows, today=self.TODAY)]
        assert order[0] == "Fabrikam-Helios"
        assert order[1] == "GOV-SYNTH00001"
        assert order[-1] == "ACME-AIDO"
        fabrikam = uiview.row_badges(rows[2], today=self.TODAY)
        assert ("closes today", "urgent") in fabrikam
        assert ("hrp: degraded", "warn") in fabrikam
        assert ("draft — clock runs, no chasing", "info") in fabrikam

    def test_fire_wins_tiebreak_over_clean_nearer(self):
        """urgent wins the tie-break: a warn row (far out + channel degraded)
        sorts after the urgent row even when it exists."""
        rows = [{"jd_id": "WARN-1", "deadline": "2026-12-30",
                 "channel_health": "degraded", "channel": "hrp"},
                {"jd_id": "FIRE-1", "deadline": "2026-10-23"}]
        order = [r["jd_id"] for r in uiview.sort_queue(rows, today=self.TODAY)]
        assert order == ["FIRE-1", "WARN-1"]


# ------------------------------------------------- keyword coverage four-state table (phase 3)


EN_JD = """# Senior Product Manager, Digital Platforms

## Requirements
- 『Product management experience』 building consumer digital products
- Demonstrated impact with **data-driven** roadmaps
- Experience with CI/CD pipelines and Kubernetes
- 『Stakeholder management』 with senior sponsors
- ISO 27001 awareness; API design

Run: apa agent run --jd docs/jd_registry_ops.py
"""

EN_MAT = """Product manager for a multimodal cockpit platform; shipped to mass
production. Owned stakeholder engagement with executives and suppliers.
Built Python services on Kubernetes with continuous integration.
ISO/SAE 21434 functional safety work. API design for partners."""


class TestKeywordCoverage:
    def test_four_states_on_english_shape(self):
        cov = uiview.keyword_coverage(EN_JD, EN_MAT)
        assert "API" in cov["covered"] and "ISO" in cov["covered"]
        assert "Stakeholder management" in cov["synonym_only"]
        assert "Product management experience" in cov["missing_have"]
        assert "Digital Platforms" in cov["missing_gap"]
        # the four states are mutually exclusive and complete: each keyword
        # lands in exactly one bucket
        allk = [k for v in cov.values() for k in v]
        assert len(allk) == len(set(allk))

    def test_path_candidates_filtered_ci_cd_kept(self):
        kws = uiview.jd_keywords(EN_JD)
        assert not any(k.endswith((".py", ".md")) for k in kws)
        assert "CI/CD" in kws

    def test_empty_material_all_gaps(self):
        cov = uiview.keyword_coverage(EN_JD, "")
        assert cov["covered"] == [] and cov["synonym_only"] == []
        assert cov["missing_gap"]           # all gaps, honest

    def test_deterministic(self):
        a = uiview.keyword_coverage(EN_JD, EN_MAT)
        b = uiview.keyword_coverage(EN_JD, EN_MAT)
        assert a == b

    def test_cjk_compat_bounded_positions(self):
        """CJK compatibility path: extract words only from bounded positions
        delimited by 『』/()/** (public-product users work mostly with
        non-Chinese data; this path is only a fallback)."""
        jd = "岗位职责：『智能座舱产品』（座舱产品线 ownership）**量产交付**"
        kws = uiview.jd_keywords(jd)
        assert "智能座舱产品" in kws
        cov = uiview.keyword_coverage(jd, "负责智能座舱产品线与量产交付")
        assert "智能座舱产品" in cov["covered"]
        assert "量产交付" in cov["covered"]

    def test_cjk_prose_not_scanned(self):
        """Prose Chinese is not scanned (without a tokenizer it would produce
        sentence fragments) — a quality bar."""
        jd = "我们负责政府数字化转型咨询经验的相关工作，包括平台建设与运维"
        kws = uiview.jd_keywords(jd)
        assert not any(uiview._CJK_RUN_RE.fullmatch(k) for k in kws)

    def test_synonym_group_user_extension(self, tmp_data, monkeypatch):
        """Users can extend synonym groups via data/keyword_synonyms.json."""
        (tmp_data / "keyword_synonyms.json").write_text(
            '[["helm", "helm chart", "package manager"]]', encoding="utf-8")
        jd = "Requirements: 『Helm』 experience"
        mat = "Deployed services with a package manager"
        cov = uiview.keyword_coverage(jd, mat)
        assert "Helm" in cov["synonym_only"]
        # the material contains the keyword verbatim → covered (synonyms do
        # not override verbatim matches)
        cov2 = uiview.keyword_coverage(jd, "Helm charts experience")
        assert "Helm" in cov2["covered"]


# ------------------------------------------------- diff review + funnel (phase 3)


class TestPackDiff:
    def test_equal_when_nothing_cut(self):
        segs = uiview.pack_diff("alpha beta gamma", "alpha beta gamma")
        assert [(op, t) for op, t in segs] == [("equal", "alpha beta gamma")]

    def test_delete_marked_when_cut(self):
        segs = uiview.pack_diff("alpha beta gamma", "alpha gamma")
        assert ("delete", "beta") in segs
        assert ("equal", "alpha") in segs and ("equal", "gamma") in segs

    def test_replace_shows_delete_then_insert(self):
        segs = uiview.pack_diff("alpha beta", "alpha delta")
        ops = [op for op, _ in segs]
        assert "delete" in ops and "insert" in ops

    def test_empty_packed_all_delete(self):
        segs = uiview.pack_diff("alpha beta", "")
        assert all(op == "delete" for op, _ in segs)


class TestFunnel:
    def test_counts_real_sources_only(self, tmp_data):
        uiview.save_queue([
            {"jd_id": "A-1", "channel": "hrp", "deadline": "2026-11-01"},
            {"jd_id": "B-2", "channel": "hrp", "deadline": "2026-11-02"},
        ])
        uiview.mark_submitted("B-2")
        uiview.record_followup("B-2")
        s = uiview.funnel_stats()
        assert s["queued"] == 2
        assert s["analyzed"] == 0      # no agent_runs / extraction_review
        assert s["material"] == 0      # no application package
        assert s["decided"] == 0
        assert s["submitted"] == 1
        assert s["engaged"] == 1

    def test_empty_queue_all_zero(self, tmp_data):
        s = uiview.funnel_stats()
        assert s == {"queued": 0, "analyzed": 0, "material": 0,
                     "decided": 0, "submitted": 0, "engaged": 0}


# ---------------------------------------------------------------- stage-4 discovery


class TestExtractJdLinks:
    MCF = "https://www.mycareersfuture.gov.sg"

    def test_mycareersfuture_relative_links_resolved(self):
        html = (
            '<a href="/job/information-technology/data-engineer-fpt-b1c6585b71b411e844e9d68fa4ae8834?source=MCF&amp;event=Search">Data Engineer</a>'
            '<a href="/job/information-technology/senior-data-engineer-zenith-a45d972478ebc485f6cd64da5f99e489">Analyst</a>'
            '<a href="https://www.mycareersfuture.gov.sg/search?search=foo">Search again</a>'
            '<a href="https://example.com/unrelated">Unrelated</a>'
        )
        items = uiview._extract_jd_links(html, "mycareersfuture", self.MCF)
        assert len(items) == 2
        assert items[0]["job_id"] == "b1c6585b71b411e844e9d68fa4ae8834"
        assert items[0]["title"] == "Data Engineer"
        assert items[0]["url"].startswith(
            "https://www.mycareersfuture.gov.sg/job/")
        assert "source=MCF&event=Search" in items[0]["url"]  # &amp; unescaped

    def test_dedup_by_job_id(self):
        html = (
            '<a href="/job/information-technology/data-engineer-fpt-b1c6585b71b411e844e9d68fa4ae8834">A</a>'
            '<a href="/job/information-technology/data-engineer-fpt-b1c6585b71b411e844e9d68fa4ae8834?source=MCF">B</a>'
        )
        items = uiview._extract_jd_links(html, "mycareersfuture", self.MCF)
        assert len(items) == 1

    def test_title_falls_back_to_url(self):
        html = '<a href="https://www.mycareersfuture.gov.sg/job/9/x"></a>'
        items = uiview._extract_jd_links(html, "mycareersfuture", self.MCF)
        assert items[0]["title"] == items[0]["url"]

    def test_unknown_channel_returns_empty(self):
        assert uiview._extract_jd_links('<a href="x">y</a>', "nope", "") == []

    def test_careersgov_link(self):
        html = ('<a href="https://www.careers.gov.sg/jobs/GOV-ABC/title">'
                "Gov Job</a>")
        items = uiview._extract_jd_links(html, "careersgov",
                                         "https://www.careers.gov.sg")
        assert len(items) == 1
        assert items[0]["job_id"] == "GOV-ABC"


class TestSearchJds:
    def _fake(self, monkeypatch, html="", error=None, text_len=3000):
        import apa.f10.browser as br
        from apa.f10.browser import MIN_BODY_CHARS

        class FakeRes:
            def __init__(self):
                self.error = error
                self.text = "x" * text_len
                self.html = html

            @property
            def rendered_ok(self):
                return self.error is None and len(self.text) >= MIN_BODY_CHARS

        class FakeBrowser:
            def __init__(self, *a, **k):
                self.last_url = None

            def fetch(self, url):
                self.last_url = url
                return FakeRes()

        monkeypatch.setattr(br, "BrowserFetch", FakeBrowser)
        return FakeBrowser

    def test_renders_query_and_extracts(self, monkeypatch):
        fb = self._fake(monkeypatch,
                        html=('<a href="https://www.mycareersfuture.gov.sg/job/111/dev">Dev</a>'
                              '<a href="https://www.mycareersfuture.gov.sg/job/222/qa">QA</a>'))
        r = uiview.search_jds("mycareersfuture", "developer")
        assert r["ok"] is True
        assert r["count"] == 2
        assert "search?search=developer" in r["search_url"]

    def test_uses_pasted_search_url(self, monkeypatch):
        self._fake(monkeypatch)
        url = "https://www.mycareersfuture.gov.sg/search?search=foo&page=2"
        r = uiview.search_jds("mycareersfuture", search_url=url)
        assert r["ok"] is True
        assert r["search_url"] == url

    def test_blocked_on_render_error(self, monkeypatch):
        self._fake(monkeypatch, error="chrome not found")
        r = uiview.search_jds("mycareersfuture", "x")
        assert r["ok"] is False
        assert r["blocked"] is True
        assert "chrome not found" in r["error"]

    def test_blocked_on_unknown_channel(self, monkeypatch):
        self._fake(monkeypatch)
        r = uiview.search_jds("nope", "x")
        assert r["ok"] is False
        assert r["blocked"] is True

    def test_blocked_when_rendered_too_little(self, monkeypatch):
        self._fake(monkeypatch, text_len=10, html="")
        r = uiview.search_jds("mycareersfuture", "x")
        assert r["ok"] is False
        assert r["blocked"] is True
        assert "too little" in r["error"]


# ---------------------------------------------------------------- resume + cover letter


class TestDraftPackage:
    def test_resume_covers_every_role(self):
        src = {"Acme · 2020-2023": "Led a team of five engineers.",
               "Beta · 2023-now": "Shipped a search service."}
        pkg = uiview.draft_package(None, src, 256)
        assert pkg["ok"] is True
        assert "Acme · 2020-2023" in pkg["resume"]
        assert "Beta · 2023-now" in pkg["resume"]
        assert "Led a team of five engineers." in pkg["resume"]
        assert "Shipped a search service." in pkg["resume"]

    def test_cover_letter_keeps_placeholders(self):
        src = {"Acme · 2020-2023": "Led a team of five engineers."}
        pkg = uiview.draft_package(None, src, None)
        assert uiview.CL_OPENING in pkg["cover_letter"]
        assert uiview.CL_CLOSING in pkg["cover_letter"]

    def test_cover_letter_invents_nothing(self):
        """no-new-claims: every sentence in the CL must exist in the source."""
        src = {"R": "Alpha sentence one. Beta sentence two. Gamma sentence three."}
        pkg = uiview.draft_package(None, src, None)
        body = pkg["cover_letter"]
        for ph in (uiview.CL_OPENING, uiview.CL_CLOSING):
            body = body.replace(ph, "")
        for s in [x.strip() for x in body.split(".") if x.strip()]:
            assert s in src["R"]

    def test_empty_source_drafts_nothing(self):
        pkg = uiview.draft_package(None, {}, 256)
        assert pkg["ok"] is False
        assert pkg["resume"] == ""
        assert pkg["cover_letter"] == ""
        assert pkg["main_role"] == ""

    def test_main_role_is_first_usable_role(self):
        src = {"": "", "Beta · 2023-now": "Shipped a search service."}
        pkg = uiview.draft_package(None, src, None)
        assert pkg["main_role"] == "Beta · 2023-now"
