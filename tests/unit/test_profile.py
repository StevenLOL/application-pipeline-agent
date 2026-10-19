"""R0 profile: ingest user documents -> single source of truth (docs/19 §4).

Test strategy: parse / extract / merge layers each tested in isolation,
**without depending on the LLM** (FakePredictor simulates the predictor
interface). Real user material is not in the tests (privacy + non-reproducible);
synthetic corpus is used instead. Equivalence of real material is verified
manually under tests/acceptance/.

Regression points pinned in this file (all are real defects found during
development):
- `locate_snippet` short-candidate threshold: 'Singapore Citizen' matching only
  'singapore' could still pass evidence capture (bare substring + long-sentence
  preference -> fact sentence attributed to wrong source);
- user-edited fields overwritten by re-ingest (violates docs/19 §4.2 iron rule 1);
- unconfirmed claims flowing into downstream material (violates docs/19 §4.3);
- scanned PDF silently producing an empty profile (should error, docs/19 §9-5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.profile import (ClaimStatus, DocFormat, Profile, UnsupportedDocument,
                         WorkAuthorization, apply_manual_edit, confirm_claim,
                         mark_needs_human, materialize_experiences, merge_claims,
                         parse_document, prune_drafts, resolve_conflict, review_queue)
from apa.profile.extract import (build_signature, extract_from_documents, hydrate_claims,
                                 locate_snippet, parse_json_resume_structured,
                                 validate_candidates)
from apa.profile.loader import _decode, detect_format, parse_dir, parse_json_resume
from apa.profile.loader import Segment
from apa.profile.models import Claim, Provenance
from apa.profile.store import history, record_change, review_queue as store_review_queue

NOW = "2026-10-19T00:00:00+00:00"


# ---- fixtures (synthetic material, no real PII) ----

@pytest.fixture
def synthetic_docx(tmp_path: Path) -> Path:
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("Contoso Ltd")
    d.add_paragraph("Senior AI Engineer")
    d.add_paragraph("Mar 2019 - Dec 2023")
    d.add_paragraph("Led the retrieval evaluation programme across 4 teams")
    d.add_paragraph("Reduced incident rate by 38%")
    d.add_paragraph("Education")
    d.add_paragraph("Nanyang Technological University")
    d.add_paragraph("Computer Engineering, Bachelor's")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "Skill"
    t.rows[0].cells[1].text = "Level"
    t.rows[1].cells[0].text = "Python"
    t.rows[1].cells[1].text = "Expert"
    p = tmp_path / "cv.docx"
    d.save(str(p))
    return p


@pytest.fixture
def synthetic_txt(tmp_path: Path) -> Path:
    p = tmp_path / "profile.txt"
    p.write_text(
        "Name: Alice Example\n"
        "Email: alice@example.test\n"
        "Citizen of Singapore\n"
        "Ministry of Manpower, Software Engineer, 2020-2022\n",
        encoding="utf-8")
    return p


@pytest.fixture
def synthetic_md(tmp_path: Path) -> Path:
    p = tmp_path / "portfolio.md"
    p.write_text(
        "# Fabrikam Robotics\n\n"
        "- Built an internal RAG evaluation harness\n"
        "- Cut retrieval p95 latency from 900ms to 210ms\n\n"
        "```python\n"
        "PROJ = 'fabrikam-rag'\n"
        "```\n",
        encoding="utf-8")
    return p


class FakePredictor:
    """Simulates DSPy Predict: returns a preset facts string (bypassing the
    ChatAdapter's secondary parsing).

    measured lesson: feeding FakeLM JSON gets re-parsed and errors out by the
    Adapter; profile reuses the same conclusion by directly simulating the
    predictor's output attribute.
    """

    def __init__(self, facts: str):
        self._facts = facts
        self.calls: list[str] = []

    def __call__(self, document: str = "", **_: object):
        self.calls.append(document)
        return type("R", (), {"facts": self._facts})()


def mk_claim(field: str, text: str, src: str = "resume.docx",
             status: ClaimStatus = ClaimStatus.DRAFT) -> Claim:
    return Claim(field=field, text=text, status=status, confidence=0.7,
                 provenance=[Provenance(src, "para 3", text[:80], NOW)])


# ================= loader =================

class TestLoader:
    def test_detect_formats_case_insensitive(self, tmp_path: Path):
        for name, want in [("a.docx", DocFormat.DOCX), ("b.PDF", DocFormat.PDF),
                           ("c.txt", DocFormat.TXT), ("d.MD", DocFormat.MD),
                           ("e.json", DocFormat.JSON_RESUME),
                           ("f.doc", DocFormat.UNSUPPORTED)]:
            assert detect_format(tmp_path / name) is want

    def test_unsupported_lists_supported_formats(self, tmp_path: Path):
        p = tmp_path / "resume.doc"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(UnsupportedDocument) as ei:
            parse_document(p)
        assert ".docx" in str(ei.value)          # error must be actionable (docs/19 §6 F4)

    def test_parse_docx_paragraphs_and_tables(self, synthetic_docx: Path):
        doc = parse_document(synthetic_docx)
        assert doc.fmt is DocFormat.DOCX
        assert "Contoso Ltd" in doc.text
        assert "Reduced incident rate by 38%" in doc.text
        assert len(doc.tables) == 1 and "Python | Expert" in doc.tables[0]
        para_locs = [s.locator for s in doc.segments]
        assert any(loc.startswith("para ") for loc in para_locs)
        assert any(loc.startswith("table 0 row") for loc in para_locs)

    def test_parse_txt_encoding_sniffing(self, synthetic_txt: Path):
        doc = parse_document(synthetic_txt)
        assert doc.fmt is DocFormat.TXT
        assert "alice@example.test" in doc.text
        assert doc.meta["encoding"].startswith("utf-8")

    def test_parse_md_keeps_code_block_content(self, synthetic_md: Path):
        doc = parse_document(synthetic_md)
        assert doc.fmt is DocFormat.MD
        assert "PROJ = 'fabrikam-rag'" in doc.text    # technical project docs often put metrics in comments

    def test_json_resume_keeps_json_pointers(self, tmp_path: Path):
        p = tmp_path / "resume.json"
        p.write_text(json.dumps({
            "basics": {"name": "Alice Example", "email": "a@b.test",
                       "location": {"city": "Singapore", "countryCode": "SG"}},
            "work": [{"name": "Contoso Ltd", "position": "Engineer",
                      "startDate": "2019-03", "endDate": "2023-12",
                      "highlights": ["Cut p95 latency 900ms -> 210ms"]}],
        }), encoding="utf-8")
        doc = parse_document(p)
        assert doc.fmt is DocFormat.JSON_RESUME
        assert any(s.locator == "/work/0/highlights/0" for s in doc.segments)

    def test_parse_dir_isolates_failures(self, tmp_path: Path):
        (tmp_path / "ok.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "bad.doc").write_text("x", encoding="utf-8")
        docs, failures = parse_dir(tmp_path)
        assert len(docs) == 1                      # 1 failure must not drag down the rest
        assert len(failures) == 1
        assert ".docx" in failures[0][1]           # failure reason is actionable

    def test_scanned_pdf_raises_instead_of_empty(self, tmp_path: Path, monkeypatch):
        """Scanned PDF must error out, **not produce an empty profile** (docs/19 §9-5)."""
        pypdf = pytest.importorskip("pypdf")

        class FakePage:
            def extract_text(self):
                return ""

        class FakeReader:
            pages = [FakePage()]

            def __init__(self, _path):
                pass

        monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
        pdf = tmp_path / "scan.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(UnsupportedDocument) as ei:
            parse_document(pdf)
        assert "OCR" in str(ei.value)             # make clear it "needs OCR" rather than "empty document"

    def test_utf16_and_latin_fallback_do_not_crash(self):
        assert "hi" in _decode("hi".encode("utf-16"))[0]
        assert _decode(b"\xff\xfe\x00")[0] is not None   # latin-1 fallback never fails


# ================= extract =================

class TestLocateSnippet:
    TEXT = ("Resume\n"
            "Nationality  Singapore  Residence  Tianjin\n"
            "Nanyang Technological University of Singapore\n"
            "Citizen of Singapore\n"
            "Led the Contoso platform team\n")

    def test_verbatim_hit_is_preferred(self):
        text = "Contoso Ltd is the employer\nLed the Contoso platform team\n"
        r = locate_snippet(text, "Contoso Ltd")
        assert r is not None and r[0] == "Contoso Ltd"
        assert text[r[1]:r[2]] == "Contoso Ltd"        # the span must actually point at that string

    def test_short_candidate_needs_all_words(self):
        """Regression: 'Singapore Citizen' matching only 'singapore' once still passed evidence capture -> attributed to wrong source."""
        text = "Nationality  Singapore  Residence  Tianjin\nNo status line here\n"
        assert locate_snippet(text, "Singapore Citizen") is None

    def test_real_two_word_hit_still_found(self):
        r = locate_snippet(self.TEXT, "Citizen Singapore")
        assert r is not None and "Citizen of Singapore" in r[0]

    def test_prefers_short_dense_sentence_over_long_partial(self):
        """Density scoring: a short full match should beat a long partial match (otherwise evidence capture attaches to the whole page)."""
        r = locate_snippet(self.TEXT, "Nanyang Technological University")
        assert r is not None
        assert len(r[0]) < 60, f"should not return the whole document: {r[0][:80]!r}"

    def test_no_substring_false_positive(self):
        assert locate_snippet(self.TEXT, "Quantum Blockchain Fellowship 2099") is None

    def test_cjk_candidate_still_locates(self):
        text = "Education background: Nanyang Technological University, Computer Engineering\nPreviously worked at a tech company\n"
        r = locate_snippet(text, "Nanyang Technological University")
        assert r is not None and "Nanyang Technological University" in r[0]

    def test_empty_candidate(self):
        assert locate_snippet(self.TEXT, "  ") is None


class TestValidateCandidates:
    def test_accepts_wellformed(self):
        ok, data, err = validate_candidates(json.dumps(
            [{"field": "identity.full_name", "text": "Alice"}]))
        assert ok and data[0]["field"] == "identity.full_name" and err == ""

    @pytest.mark.parametrize("payload,expect", [
        ("not json at all", "not valid JSON"),
        ('{"field":"a","text":"b"}', "expected a JSON array"),
        (json.dumps([{"field": "bogus", "text": "x"}]), "unknown field"),
        (json.dumps([{"field": "identity.email", "text": "  "}]), "empty text"),
        (json.dumps([{"field": "identity.work_authorization", "text": "wizard"}]),
         "invalid work_authorization"),
        (json.dumps([{"text": "x"}]), "missing 'field'"),
    ])
    def test_rejects(self, payload, expect):
        ok, data, err = validate_candidates(payload)
        assert not ok and expect in err and data is None

    def test_no_partial_adoption(self):
        """A single illegal entry rejects the whole batch -- partial adoption would break review-queue explainability."""
        ok, data, err = validate_candidates(json.dumps([
            {"field": "identity.full_name", "text": "Alice"},
            {"field": "bogus", "text": "x"},
        ]))
        assert not ok and data is None

    def test_oversized_list_rejected(self):
        payload = json.dumps([{"field": "skills.0", "text": f"s{i}"} for i in range(500)])
        ok, _, err = validate_candidates(payload)
        assert not ok and "suspiciously large" in err


class TestHydrateClaims:
    @pytest.fixture
    def doc(self, synthetic_txt: Path):
        return parse_document(synthetic_txt)

    def test_located_claim_carries_provenance(self, doc):
        claims, dropped = hydrate_claims(
            doc, [{"field": "identity.email", "text": "alice@example.test"}], NOW)
        assert not dropped and len(claims) == 1
        p = claims[0].provenance[0]
        assert p.source_file == "profile.txt" and p.locator.startswith("chars ")
        assert claims[0].confidence >= 0.7

    def test_unlocatable_candidate_is_dropped(self, doc):
        """Hallucinated candidates are dropped directly: fact sentences without source backing do not enter the single source of truth."""
        claims, dropped = hydrate_claims(
            doc, [{"field": "skills.0", "text": "Kubernetes operator"}], NOW)
        assert claims == [] and len(dropped) == 1

    def test_snippet_is_truncated_for_storage(self, synthetic_txt: Path):
        doc = parse_document(synthetic_txt)
        claims, _ = hydrate_claims(
            doc, [{"field": "identity.email", "text": "alice@example.test"}], NOW)
        assert len(claims[0].provenance[0].snippet) <= 400


class TestExtractFromDocuments:
    def test_long_document_is_chunked(self, synthetic_docx: Path):
        """Regression (measured 2026-10-19): a 16526-char resume fed to 1.5B at once -> truncated response, zero extraction."""
        from apa.profile.extract import chunk_document

        doc = parse_document(synthetic_docx)
        doc.text = "\n".join(s.text for s in doc.segments) * 30   # ~scaled up to a long document
        doc.segments = [Segment(text=f"line {i} of a long resume " + "x" * 40,
                                locator=f"para {i}") for i in range(200)]
        doc.text = "\n".join(s.text for s in doc.segments)
        blocks = chunk_document(doc, budget=800)
        assert len(blocks) > 1
        assert all(len(b.text) <= 800 * 2 for b, _, _ in blocks)   # still bounded after overlap

    def test_chunks_carry_segments_for_locator(self, synthetic_txt: Path):
        from apa.profile.extract import chunk_document

        doc = parse_document(synthetic_txt)
        doc.segments = [Segment(text=f"statement number {i} " + "y" * 30,
                                locator=f"para {i}") for i in range(60)]
        doc.text = "\n".join(s.text for s in doc.segments)
        blocks = chunk_document(doc, budget=400)
        assert blocks and all(b.segments for b, _, _ in blocks)
        assert all(note for _, _, note in blocks)                 # each block has a readable annotation

    def test_short_document_is_one_block(self, synthetic_txt: Path):
        from apa.profile.extract import chunk_document

        doc = parse_document(synthetic_txt)
        blocks = chunk_document(doc, budget=100000)
        assert len(blocks) == 1 and blocks[0][2] == "whole document"

    def test_overlap_dedupes_duplicate_claims(self, synthetic_txt: Path):
        """Overlap between adjacent blocks -> same sentence extracted twice -> must dedupe (otherwise duplicate experiences appear in the profile)."""
        payload = json.dumps([{"field": "identity.email",
                               "text": "alice@example.test"}])
        doc = parse_document(synthetic_txt)
        segs = [Segment(text=f"line {i} " + "z" * 30, locator=f"para {i}")
                for i in range(40)]
        segs.append(Segment(text="Contact: alice@example.test", locator="para 40"))
        doc.segments = segs
        doc.text = "\n".join(s.text for s in segs)
        rep = extract_from_documents([doc], FakePredictor(payload), NOW, budget=500)
        emails = [c for c in rep.claims if c.field == "identity.email"]
        assert len(emails) == 1

    def test_lm_failure_does_not_kill_batch(self, synthetic_txt: Path):
        class Boom:
            def __call__(self, document: str = "", **_: object):
                raise RuntimeError("llama.cpp unreachable")

        doc = parse_document(synthetic_txt)
        rep = extract_from_documents([doc], Boom(), NOW)
        assert rep.claims == [] and "llama.cpp unreachable" in rep.dropped[0]["text"]

    def test_bad_json_marks_document_dropped(self, synthetic_txt: Path):
        doc = parse_document(synthetic_txt)
        rep = extract_from_documents([doc], FakePredictor("garbage"), NOW)
        assert rep.documents == [] and rep.dropped_count == 1

    def test_happy_path_with_fake_predictor(self, synthetic_txt: Path):
        doc = parse_document(synthetic_txt)
        payload = json.dumps([{"field": "identity.email",
                               "text": "alice@example.test"}])
        rep = extract_from_documents([doc], FakePredictor(payload), NOW)
        assert rep.documents == ["profile.txt"] and len(rep.claims) == 1

    def test_signature_is_importable(self):
        sig = build_signature()
        assert hasattr(sig, "instructions")


class TestBackendCapabilityGate:
    """**Discipline regression**: a weak tier must be blocked before the call,
    not patched afterwards into something that "looks usable".

    Based on reports/f3/run_001.md §2.3 (measured 5-run): Qwen2.5-1.5B's list
    extraction is a consistently failing mode (requirements schema_ok_rate 0/5,
    assessments always []), and prompt engineering is ineffective -> this is a
    capability boundary. Profile fact extraction is a list task, so 1.5B is
    unusable -- the tool must explicitly refuse and must not cobble a result
    out of truncated output.
    """

    def test_15b_is_refused_for_list_extraction(self):
        from apa.profile.extract import BackendTooWeak, check_backend_capability

        with pytest.raises(BackendTooWeak) as ei:
            check_backend_capability("qwen2.5-1.5b-instruct")
        msg = str(ei.value)
        assert "list extraction" in msg
        assert "run_001.md" in msg             # points back to measured evidence, not an empty assertion
        assert "APA_LLM_MODEL" in msg          # gives an actionable next step

    @pytest.mark.parametrize("model", [
        "qwen2.5-7b-instruct", "granite4.2:3b", "claude-sonnet-5",
        "gpt-oss-20b", "/models/llama-3.1-8b.gguf",
    ])
    def test_stronger_models_pass(self, model):
        from apa.profile.extract import check_backend_capability

        check_backend_capability(model)        # not raising = pass

    def test_case_insensitive(self):
        from apa.profile.extract import BackendTooWeak, check_backend_capability

        with pytest.raises(BackendTooWeak):
            check_backend_capability("E:/dev/models/QWEN2.5-1.5B-Instruct-Q4_K_M.gguf")

    def test_cli_ingest_refuses_on_weak_backend(self, tmp_path: Path, monkeypatch):
        """End-to-end: the CLI must refuse before extraction and return exit code 3 (distinct from 2 = LLM unreachable)."""
        from apa.cli import main
        from apa.llm.client import LLMSettings

        # Fabricate a "1.5B online" state -- do not depend on whether a real
        # llama.cpp service is up (environment decoupling);
        # the real gate check_backend_capability runs, not a mocked assertion.
        # (Fixed 2026-10-19: the original assumed a service on 8080; when the
        # service stopped it degraded into a false exit-2 failure.)
        monkeypatch.setattr(
            "apa.llm.client.load_settings",
            lambda **kw: LLMSettings(
                backend="local", base_url="http://127.0.0.1:8080/v1",
                api_key="not-used", model="qwen2.5-1.5b-instruct",
                cloud_allowed=False),
        )

        doc = tmp_path / "cv.txt"
        doc.write_text("Contoso Ltd\nEngineer\n", encoding="utf-8")
        rc = main(["profile", "ingest", str(tmp_path / "cv.txt"),
                   "--profile", str(tmp_path / "p.json")])
        assert rc == 3

    def test_json_resume_needs_no_llm(self, tmp_path: Path):
        """JSON Resume uses structured direct extraction -- **no LLM needed at all**, so it is not subject to the tier gate."""
        p = tmp_path / "resume.json"
        p.write_text(json.dumps({"basics": {"name": "Alice Example"},
                                 "work": [{"name": "Contoso Ltd",
                                           "highlights": ["Cut p95 by 78%"]}]}),
                     encoding="utf-8")
        from apa.cli import main

        prof = tmp_path / "p.json"
        assert main(["profile", "ingest", str(p), "--profile", str(prof)]) == 0
        data = json.loads(prof.read_text(encoding="utf-8"))
        assert data["claims"]["identity.full_name"]["text"] == "Alice Example"
        assert data["claims"]["experiences.0.bullets.0"]["text"] == "Cut p95 by 78%"


class TestJsonResumeNative:
    def test_structured_extraction_without_llm(self, tmp_path: Path):
        p = tmp_path / "resume.json"
        p.write_text(json.dumps({
            "basics": {"name": "Alice Example", "summary": "AI engineer",
                       "workAuthorization": "sg_citizen"},
            "work": [{"name": "Contoso Ltd", "position": "Engineer",
                      "highlights": ["Cut p95 latency 900ms -> 210ms"]}],
            "education": [{"institution": "NBU", "studyType": "Master's"}],
            "skills": [{"name": "Python", "keywords": ["DSPy", "faiss"]}],
        }), encoding="utf-8")
        doc = parse_document(p)
        claims = parse_json_resume_structured(doc, NOW)
        fields = {c.field for c in claims}
        assert "identity.full_name" in fields
        assert "experiences.0.bullets.0" in fields
        assert "education.0.institution" in fields
        assert "skills.0.keywords.1" in fields
        assert all(c.confidence == 0.9 for c in claims)

    def test_rejects_non_json_resume_document(self, synthetic_txt: Path):
        with pytest.raises(ValueError):
            parse_json_resume_structured(parse_document(synthetic_txt), NOW)


# ================= merge (three iron rules) =================

class TestMergeIronRules:
    def test_rule1_manual_override_never_overwritten(self):
        """Iron rule 1: user correction > new extraction (the core guarantee that the user "has a correction need")."""
        p = Profile()
        merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd")])
        apply_manual_edit(p, "experiences.0.org", "Contoso Ltd (formerly Fabrikam)",
                         reason="merged entities")
        out = merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd", "cv2.pdf")])
        assert out.skipped_manual == ["experiences.0.org"]
        assert p.claims["experiences.0.org"].text == "Contoso Ltd (formerly Fabrikam)"
        assert p.claims["experiences.0.org"].status is ClaimStatus.MANUAL_OVERRIDE

    def test_rule2_refinement_appends_not_rewrites(self):
        """Iron rule 2: refinement -> adopt new value but keep the old sentence as a trace (downstream may already reference the old sentence)."""
        p = Profile()
        merge_claims(p, [mk_claim("skills.0", "Presidio PII blocking")])
        out = merge_claims(p, [mk_claim(
            "skills.0", "Presidio PII blocking for NRIC and passport numbers")])
        assert out.refined == ["skills.0"]
        assert "NRIC" in p.claims["skills.0"].text
        assert len(p.claims["skills.0"].rejected_sources) == 1   # old value not lost

    def test_rule3_conflict_is_never_silent(self):
        """Iron rule 3: conflicts are never last-write-wins; wait for human ruling."""
        p = Profile()
        merge_claims(p, [mk_claim("identity.location", "Tianjin")])
        out = merge_claims(p, [mk_claim("identity.location", "Singapore", "cv2.pdf")])
        assert out.conflicts == ["identity.location"]
        assert p.claims["identity.location"].text == "Tianjin"          # old value not changed
        assert p.claims["identity.location"].status is ClaimStatus.CONFLICT
        assert p.claims["identity.location"].rejected_sources           # new source leaves a trace

    def test_multi_source_reinforcement_raises_confidence(self):
        p = Profile()
        merge_claims(p, [mk_claim("identity.full_name", "Alice Example", "a.docx")])
        base = p.claims["identity.full_name"].confidence
        merge_claims(p, [mk_claim("identity.full_name", "Alice Example", "b.pdf")])
        claim = p.claims["identity.full_name"]
        assert claim.confidence > base
        assert {x.source_file for x in claim.provenance} == {"a.docx", "b.pdf"}

    def test_confidence_is_capped(self):
        p = Profile()
        for i in range(10):
            merge_claims(p, [mk_claim("identity.full_name", "Alice Example", f"s{i}.txt")])
        assert p.claims["identity.full_name"].confidence <= 0.95

    def test_short_strings_are_not_treated_as_refinement(self):
        """'NBU' vs 'NBU CSC' should not be judged as refinement (12-char threshold) -- better to ask once more."""
        p = Profile()
        merge_claims(p, [mk_claim("education.0.institution", "NBU")])
        out = merge_claims(p, [mk_claim("education.0.institution", "NBU Computer Science")])
        assert out.conflicts and not out.refined


class TestUserActions:
    @pytest.fixture
    def p(self) -> Profile:
        p = Profile()
        merge_claims(p, [mk_claim("identity.full_name", "Alice")])
        return p

    def test_confirm_draft(self, p):
        c = confirm_claim(p, "identity.full_name")
        assert c.status is ClaimStatus.CONFIRMED and c.confidence >= 0.8

    def test_confirm_missing_field_raises(self, p):
        with pytest.raises(KeyError):
            confirm_claim(p, "identity.nope")

    def test_manual_edit_keeps_old_value_with_reason(self, p):
        apply_manual_edit(p, "identity.full_name", "Alice B", reason="legal name")
        old = [x for x in p.claims["identity.full_name"].rejected_sources
               if "manual-superseded" in x.extractor]
        assert old and "legal name" in old[0].extractor and old[0].snippet == "Alice"

    @pytest.mark.parametrize("bad", ["", "   "])
    def test_manual_edit_rejects_blank(self, p, bad):
        with pytest.raises(ValueError):
            apply_manual_edit(p, "identity.full_name", bad)

    def test_resolve_conflict_take_incoming(self):
        p = Profile()
        merge_claims(p, [mk_claim("identity.location", "Tianjin")])
        merge_claims(p, [mk_claim("identity.location", "Singapore", "cv2.pdf")])
        c = resolve_conflict(p, "identity.location", "incoming")
        assert c.text == "Singapore" and c.status is ClaimStatus.CONFIRMED

    def test_resolve_conflict_take_existing(self):
        p = Profile()
        merge_claims(p, [mk_claim("identity.location", "Tianjin")])
        merge_claims(p, [mk_claim("identity.location", "Singapore", "cv2.pdf")])
        c = resolve_conflict(p, "identity.location", "existing")
        assert c.text == "Tianjin" and c.rejected_sources == []

    def test_resolve_conflict_rejects_bad_take(self):
        p = Profile()
        merge_claims(p, [mk_claim("identity.location", "Tianjin")])
        merge_claims(p, [mk_claim("identity.location", "Singapore", "cv2.pdf")])
        with pytest.raises(ValueError):
            resolve_conflict(p, "identity.location", "whatever")

    def test_needs_human_marker(self, p):
        c = mark_needs_human(p, "identity.full_name")
        assert c.status is ClaimStatus.NEEDS_HUMAN


class TestMaterializeGate:
    def test_unconfirmed_never_enters_materials(self):
        """docs/19 §4.3: draft/conflict/needs_human never enter downstream material."""
        p = Profile()
        merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd"),
                         mk_claim("experiences.0.title", "Engineer"),
                         mk_claim("experiences.0.bullets.0", "Cut p95 by 78%")])
        materialize_experiences(p)
        assert p.experiences == []
        assert p.confirmed_claims() == []

    def test_confirmed_materializes_with_bullets(self):
        p = Profile()
        merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd"),
                         mk_claim("experiences.0.title", "Engineer"),
                         mk_claim("experiences.0.bullets.0", "Cut p95 by 78%"),
                         mk_claim("experiences.0.bullets.1", "Led 4 engineers")])
        for f in list(p.claims):
            confirm_claim(p, f)
        materialize_experiences(p)
        assert len(p.experiences) == 1
        assert len(p.experiences[0].bullets) == 2
        assert p.experiences[0].org == "Contoso Ltd"

    def test_bullet_without_owner_is_not_materialized(self):
        p = Profile()
        merge_claims(p, [mk_claim("experiences.0.bullets.0", "orphan achievement")])
        confirm_claim(p, "experiences.0.bullets.0")
        materialize_experiences(p)
        assert p.experiences == []          # orphan sentence is unusable


class TestPrune:
    def test_prunes_only_stale_drafts(self):
        p = Profile()
        stale = mk_claim("skills.0", "Kubernetes", "old.txt")
        stale.provenance[0] = Provenance("old.txt", "chars 0-10", "Kubernetes",
                                         "2026-01-01T00:00:00+00:00")
        p.claims["skills.0"] = stale
        pinned = mk_claim("skills.1", "Terraform", "old.txt")
        pinned.status = ClaimStatus.MANUAL_OVERRIDE
        p.claims["skills.1"] = pinned
        dropped = prune_drafts(p)
        assert dropped == ["skills.0"] and "skills.1" in p.claims


# ================= store / review queue =================

class TestStoreAndReview:
    def test_review_queue_prioritises_blocking_items(self):
        p = Profile()
        merge_claims(p, [mk_claim("skills.0", "Kubernetes"),
                         mk_claim("identity.location", "Singapore"),
                         mk_claim("identity.email", "a@b.test"),
                         mk_claim("identity.phone", "+65 6000 0000")])
        merge_claims(p, [mk_claim("identity.phone", "+65 9999 9999", "other.txt")])
        rows = store_review_queue(p)
        assert rows[0]["status"] in ("conflict", "needs_human")
        assert rows[0]["has_conflict"] is True

    def test_pinned_fields_listed(self):
        p = Profile()
        merge_claims(p, [mk_claim("identity.full_name", "Alice")])
        apply_manual_edit(p, "identity.full_name", "Alice B")
        assert p.pinned_fields() == ["identity.full_name"]

    def test_history_is_append_only(self, tmp_path: Path):
        log = tmp_path / "hist.jsonl"
        record_change("edit", "identity.full_name", "A", "B", "typo", path=log)
        record_change("confirm", "identity.email", "", "a@b.test", path=log)
        rows = history(path=log)
        assert len(rows) == 2
        assert rows[0]["old"] == "A" and rows[1]["action"] == "confirm"
        assert log.read_text(encoding="utf-8").count("\n") == 2

    def test_history_tolerates_corrupt_line(self, tmp_path: Path):
        log = tmp_path / "hist.jsonl"
        record_change("edit", "f", "a", "b", path=log)
        with log.open("a", encoding="utf-8") as f:
            f.write("{broken\n")
        assert len(history(path=log)) == 1


# ================= WorkAuthorization (identity-related) =================

class TestWorkAuthorization:
    @pytest.mark.parametrize("text,want", [
        ("Citizen of Singapore", WorkAuthorization.CITIZEN),
        ("Singapore Citizen since 1990", WorkAuthorization.CITIZEN),
        ("I am a Permanent Resident of Singapore", WorkAuthorization.PR),
        ("Employment Pass holder", WorkAuthorization.EP),
        ("Holding an S Pass", WorkAuthorization.S_PASS),
        ("Married, two children", WorkAuthorization.NOT_STATED),
        ("", WorkAuthorization.NOT_STATED),
    ])
    def test_from_text(self, text, want):
        assert WorkAuthorization.from_text(text) is want

    def test_never_defaults_to_citizen(self):
        """Not stated != citizen -- defaulting to Citizen fabricates an identity field (class A4 incident)."""
        assert WorkAuthorization.from_text("Born in Tianjin") is WorkAuthorization.NOT_STATED

    def test_labels_exist_for_every_member(self):
        for w in WorkAuthorization:
            assert w.label


# ================= Profile round-trip =================

class TestRoundTrip:
    def test_dict_round_trip_preserves_provenance_and_status(self):
        p = Profile()
        merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd")])
        merge_claims(p, [mk_claim("experiences.0.org", "Contoso Ltd", "cv2.pdf")])
        p2 = Profile.from_dict(json.loads(json.dumps(p.to_dict())))
        c = p2.claims["experiences.0.org"]
        assert c.status is ClaimStatus.DRAFT
        assert {x.source_file for x in c.provenance} == {"resume.docx", "cv2.pdf"}
        assert c.confidence == p.claims["experiences.0.org"].confidence
