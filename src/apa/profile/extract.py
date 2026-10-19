"""R0 Fact extraction: Document -> Claim[], each with provenance (docs/19 sec 4.2 (3)).

**Division-of-labor discipline** (carrying forward the empirical lessons of D9, docs/10 D9):
the model **only discovers candidates**; "verbatim quotes are located in the source text by
the code layer" — we do not let the model perform the triple task of "extract + locate +
transcribe" simultaneously (a 1.5B model reliably escapes under the triple task). Candidates
whose provenance cannot be located are **dropped outright**: fact sentences unsupported by the
source do not enter the single source of truth (this is the mechanized safeguard against
accident 4, mirrored on the profile side).

Two paths:
- ``extract_facts_candidates``: DSPy Signature (requires an LM); tests use FakeLM/FakePredictor
- ``parse_json_resume_structured``: direct structured extraction (no LLM needed; JSON Resume is
  already structured)

Confidence model (deterministic, unit-testable):
- has provenance and verbatim hit in source: starts at 0.7
- corroborated by multiple sources (same value from different files): +0.1 each time, capped at 0.95
- location failed / text not in source: dropped (does not enter needs_human — a guess with no
  provenance should not even be displayed)
- direct structured extraction (JSON Resume): 0.9 (no model uncertainty)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .loader import Document, Segment
from .models import Claim, ClaimStatus, DocFormat, Provenance, WorkAuthorization

_EXTRACTOR = "profile-extract-v1"


# ---- Code-layer location (deterministic; same idea as extract/extractor._locate_quote) ----

_SENT_SPLIT = re.compile(r"(?<=[.!?\n])\s+")


def _word_present(word: str, haystack_low: str) -> bool:
    """Whether a word appears: **requires a word-start boundary**, allowing morphological
    suffixes (national -> nationality).

    A bare ``word in text`` would let "national" match "Nationality" — on Singapore/Chinese
    resumes this is a frequent source of misjudgment (e.g. "National University of Singapore"
    vs "Nationality: Singapore") -> extracted fact sentences get attached to the wrong
    provenance, which is worse than failing to extract (the review panel would treat it as
    a verified fact).
    """
    return re.search(rf"\b{re.escape(word)}\w*", haystack_low) is not None


def _required_hits(n_words: int, min_ratio: float) -> int:
    """Hit threshold: **short candidates require all words to hit** (forensic intuition:
    a 3-word candidate that hits only 1 word ~= not found).

    The previous single-ratio threshold produced real false positives: for a 2-word candidate
    'Singapore Citizen' the threshold was ``max(1, 2*0.5)=1``, so a candidate where "citizen"
    never appears and only "singapore" hits could still pass (in testing it located
    "Nationality Singapore Residence Tianjin"). On a PDF this means **a fact sentence attached
    to the wrong provenance** — worse than failing to extract.
    """
    if n_words <= 4:
        return n_words                 # 1-4 words: all must hit
    return max(1, int(n_words * min_ratio))


def locate_snippet(doc_text: str, candidate: str, min_ratio: float = 0.5) -> Optional[str]:
    """Locate a verbatim quote of the candidate in the source text.

    Score = ``number of matched content words / sqrt(sentence length)`` (**density**, not
    absolute hit count): selecting purely by hit count would favor long sentences. A density
    score lets "short and fully matched" beat "long and half matched". The threshold uses
    ``_required_hits`` (short candidates must fully match).
    """
    c = (candidate or "").strip()
    if not c:
        return None
    if c in doc_text:                       # verbatim hit preferred (highest confidence)
        i = doc_text.index(c)
        return c, i, i + len(c)

    sentences = [(m.start(), m.group(0).strip()) for m in
                 re.finditer(r"[^\n]*[.!?\n][^\n]*|[^\n]+", doc_text)]
    words = {w for w in re.findall(r"[a-z]+", c.lower()) if len(w) > 3}
    if not words:
        return None
    need = _required_hits(len(words), min_ratio)
    best: Optional[tuple[str, int, int]] = None
    best_score = 0.0
    for start, s in sentences:
        s = s.strip()
        if not s:
            continue
        low = s.lower()
        hits = sum(1 for w in words if _word_present(w, low))
        if hits < need:
            continue
        score = hits / (len(s) ** 0.5)
        if score > best_score:
            off = doc_text.find(s, start)
            best, best_score = (s, off, off + len(s)), score
    return best


def _segment_for(doc: Document, char_start: int, char_end: int) -> str:
    """Given a character span -> a segment-level locator (para N / page N / table N row M ...).

    When multiple segments match, take the **shortest** one (the user wants the segment closest
    to the fact, not the whole page).
    """
    covering = [s for s in doc.segments if char_start >= s._start and char_end <= s._end]
    if not covering:
        return f"chars {char_start}-{char_end}"
    seg = min(covering, key=lambda s: s._end - s._start)
    return seg.locator


def _attach_segments(doc: Document) -> Document:
    """Compute the character span for each segment (internal index, not persisted)."""
    cursor = 0
    text = doc.text
    for seg in doc.segments:
        idx = text.find(seg.text, cursor)
        if idx < 0:
            idx = text.find(seg.text)
        if idx < 0:
            seg._start, seg._end = cursor, cursor
        else:
            seg._start, seg._end = idx, idx + len(seg.text)
            cursor = seg._end
    return doc


# ---- DSPy Signature ----

_SIGNATURE_TEXT = """
Extract structured facts from the candidate's own resume document. Only extract what the
document explicitly states — do not infer, do not fill in gaps, do not polish. Copy names /
companies / titles / schools / dates / locations verbatim from the source.

Output a JSON array, each element of the form:
  {{"field": "<category>.<field>", "text": "<source fragment>"}}

Allowed field values (strict):
  identity.full_name / identity.email / identity.phone / identity.location /
  identity.work_authorization / education.<n>.institution / education.<n>.degree /
  experiences.<n>.org / experiences.<n>.title / experiences.<n>.start /
  experiences.<n>.end / experiences.<n>.location / experiences.<n>.category /
  experiences.<n>.bullets.<m> / skills.<n>

text must be a fragment that **appears verbatim** in the document (do not rewrite, translate,
or merge). For work_authorization, text must be exactly one of "sg_citizen" /
"permanent_resident" / "employment_pass" / "s_pass" / "not_stated".
For bullets and skills, write the complete achievement sentences from the document (including
numbers).
"""

#: The backend capability tier required by this task. **List extraction** (a JSON array with
#: multiple entries) — per reports/f3/run_001.md sec 2.3: Qwen2.5-1.5B is a **consistently
#: failing mode** for list extraction (prompt engineering does not help; it is a capability
#: boundary — single values work, lists do not).
#:
#: This is not bias against 1.5B, but a conclusion D9 pinned down with a 5-run empirical test.
#: Profile fact extraction is a list task, so it **must** run on a backend stronger than 1.5B —
#: a strong local model / Ollama / the cloud private tiers (docs/15 In-Region discipline).
#: See ``check_backend_capability``.
CAPABILITY_LIST_EXTRACTION = "list_extraction"

#: Model prefixes judged "known insufficient" (empirically recorded, not guessed).
#: New entries must be accompanied by a measured report number under reports/.
_WEAK_FOR_LISTS = ("qwen2.5-1.5b",)


def build_signature():
    """Construct the DSPy Signature (lazy import: dspy is only loaded when an LM is needed)."""
    import dspy

    class ExtractProfileFacts(dspy.Signature):
        """Extract fact items from candidate documents; each item's text must be verbatim from the source."""

        document: str = dspy.InputField(desc="full text of candidate resume/project document")
        facts: str = dspy.OutputField(desc=_SIGNATURE_TEXT.strip())

    return ExtractProfileFacts


class BackendTooWeak(RuntimeError):
    """Backend capability is insufficient for this task — **explicitly refuse**, do not
    degrade to produce incomplete results.

    Design rationale: a corollary of the docs/18 tiered testing strategy "must not be
    interchangeable" — since D9 has empirically established that 1.5B's list extraction is a
    capability boundary, we should block it **before the call**, rather than after the call use
    chunking/patching to cobble incomplete output into something that looks usable. The latter
    manufactures an illusion of "usable", exactly the kind of thing the "numbers generated by CI
    / conclusions must have evidence" discipline guards against.
    """


def check_backend_capability(model: str) -> None:
    """Check whether the backend supports list extraction based on empirical records; raises
    :class:`BackendTooWeak` if it does not meet the bar."""
    low = (model or "").lower()
    for weak in _WEAK_FOR_LISTS:
        if weak in low:
            raise BackendTooWeak(
                f"model {model!r} cannot do list extraction (measured, "
                f"reports/f3/run_001.md §2.3: single-value OK, lists fail "
                f"consistently). Point APA_LLM_MODEL at a stronger backend — a "
                f"larger local GGUF, Ollama (e.g. granite4.2:3b), or the cloud "
                f"private tiers in docs/15 (In-Region). To ingest without an "
                f"LLM at all, supply a JSON Resume: apa profile ingest --no-llm")


# ---- Extraction result validation (deterministic) ----

_ALLOWED_FIELDS = re.compile(
    r"^(identity\.(full_name|email|phone|location|work_authorization)"
    r"|education\.\d+\.(institution|degree)"
    r"|experiences\.\d+\.(org|title|start|end|location|category)"
    r"|experiences\.\d+\.bullets\.\d+"
    r"|skills\.\d+)$"
)

_VALID_WA = {w.value for w in WorkAuthorization}


def _try_json(text: str) -> Optional[Any]:
    return _try_json_loose(text)[0]


def _try_json_loose(text: str) -> tuple[Optional[Any], bool]:
    """Parse model JSON. Returns (value, whether truncation repair was applied).

    **Why a loose mode is needed** (measured 2026-10-19): the local 1.5B model, on fact
    extraction, loops repeatedly until the output is truncated by max_tokens, leaving a
    half-record like ``{"field": "experiences.3.title"`` at the tail. A strict ``json.loads``
    would void the **entire batch** — even though the previous dozen-odd records were good.

    Repair strategy (conservative): only accept complete elements **before the first
    unclosed array element**; never guess to complete truncated content. The dropped part is
    recorded in ``dropped`` by the caller of ``validate_candidates``, so explainability is
    preserved.
    """
    try:
        return json.loads(text), False
    except (json.JSONDecodeError, TypeError):
        pass

    start = text.find("[")
    if start < 0:
        return None, False
    items: list[Any] = []
    decoder = json.JSONDecoder()
    pos = start + 1
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError:
            break                          # truncated element -> stop here, drop it
        items.append(obj)
        pos = end
    return (items, True) if items else (None, False)


def validate_candidates(value: str) -> tuple[bool, Any, str]:
    """Validate model output: JSON array + valid field + non-empty text.

    Returns (ok, data, error). Any violation rejects the whole batch — **no partial adoption**
    (partial adoption would make "what was dropped" unknowable, breaking the review panel's
    explainability).
    """
    data, repaired = _try_json_loose(value)
    if data is None:
        return False, None, "not valid JSON: no parsable array"
    if not isinstance(data, list):
        return False, None, f"expected a JSON array, got {type(data).__name__}"
    if len(data) > 40:
        return False, None, f"suspiciously large candidate list ({len(data)})"
    out: list[dict[str, str]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return False, None, f"item[{i}] must be an object"
        f, t = item.get("field"), item.get("text")
        if not isinstance(f, str) or not f.strip():
            return False, None, f"item[{i}] missing 'field'"
        if not _ALLOWED_FIELDS.match(f.strip()):
            return False, None, f"item[{i}] unknown field {f!r}"
        if not isinstance(t, str) or not t.strip():
            return False, None, f"item[{i}] has empty text (field={f!r})"
        if f.strip().endswith("work_authorization") and t.strip() not in _VALID_WA:
            return False, None, f"item[{i}] invalid work_authorization {t!r}"
        out.append({"field": f.strip(), "text": t.strip()})
    return True, out, ""


def hydrate_claims(doc: Document, candidates: list[dict[str, str]],
                   now: str) -> tuple[list[Claim], list[dict[str, str]]]:
    """Candidate -> Claim with provenance. **Candidates whose source cannot be located are dropped**.

    Returns (claims, dropped). dropped records the dropped candidates — this is part of
    explainability: the review panel must be able to answer "why didn't this one come in"
    (the answer is usually "this sentence isn't in the document").
    """
    _attach_segments(doc)
    doc.text = doc.text  # segments already carry spans
    claims: list[Claim] = []
    dropped: list[dict[str, str]] = []
    for c in candidates:
        located = locate_snippet(doc.text, c["text"])
        if located is None:
            dropped.append(c)
            continue
        snippet, start, end = located
        claims.append(Claim(
            field=c["field"],
            text=snippet,
            status=ClaimStatus.DRAFT,
            confidence=0.7,
            provenance=[Provenance(
                source_file=doc.path.name,
                locator=_segment_for(doc, start, end),
                snippet=snippet[:400],
                extracted_at=now,
                extractor=_EXTRACTOR,
            )],
        ))
    return claims, dropped


@dataclass
class ExtractionReport:
    """Extraction result + explainability metadata (audit surface: docs/08 sec 6 "low-confidence extraction" HITL)."""

    claims: list[Claim]
    dropped: list[dict[str, str]]
    documents: list[str]

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def chunk_document(doc: Document, budget: int = 1200,
                   overlap_segments: int = 2) -> list[tuple[Document, str, str]]:
    """Split a long document into per-chunk extractable fragments. Returns [(sub-Document,
    start locator, note)].

    **Why chunking is required** (measured 2026-10-19): feeding the 16526-character AllInOne
    resume to the local 1.5B model in one shot resulted in truncated LM responses, incomplete
    JSON, and zero extraction across the whole document. The local llama.cpp n_ctx=4096
    (docs/09 hardware reality) determines the single-input upper bound.

    Splitting method: accumulate segments until the budget is reached, with cut points falling
    on segment boundaries (never mid-sentence — cutting would make `_segment_for`'s locator
    point to half a paragraph); adjacent chunks keep ``overlap_segments`` segments of overlap to
    avoid a sentence spanning chunks being severed.

    Each chunk is still a **text subset of the same doc**; `hydrate_claims` locates within the
    chunk: a long sentence spanning chunks (the same sentence cut in half) that cannot be
    located is dropped — better to extract one fewer than to attach to the wrong provenance
    (same forensic discipline as docs/19 sec 4.2).
    """
    if len(doc.text) <= budget:
        return [(doc, "", "whole document")]
    groups: list[list] = []
    cur: list = []
    cur_len = 0
    for seg in doc.segments:
        seg_len = len(seg.text) + 1
        if cur and cur_len + seg_len > budget:
            groups.append(cur)
            cur = cur[-overlap_segments:] if overlap_segments else []
            cur_len = sum(len(s.text) + 1 for s in cur)
        cur.append(seg)
        cur_len += seg_len
    if cur:
        groups.append(cur)

    out: list[tuple[Document, str, str]] = []
    total = len(groups)
    for i, segs in enumerate(groups, start=1):
        text = "\n".join(s.text for s in segs)
        sub = Document(path=doc.path, fmt=doc.fmt, text=text,
                       segments=list(segs), tables=doc.tables, meta=doc.meta)
        note = "whole document" if total == 1 else f"chunk {i}/{total}"
        out.append((sub, "", note))
    return out


def extract_from_documents(docs: list[Document], predictor: Any, now: str,
                           max_chars: int = 24000, budget: int = 1200) -> ExtractionReport:
    """Multiple documents -> Claim[] (predictor is a DSPy Predict or FakePredictor).

    ``max_chars``: per-document character cap; ``budget``: **per-chunk** extraction budget
    (a real constraint of the local 1.5B n_ctx=4096; see ``chunk_document``'s measured notes).

    Cross-chunk dedup: adjacent chunks share segment overlap, so the same sentence is extracted
    once per chunk -> dedup by (field, normalized text), keeping the one with higher confidence
    / more provenance.
    """
    all_claims: list[Claim] = []
    dropped: list[dict[str, str]] = []
    used: list[str] = []
    seen: dict[tuple[str, str], Claim] = {}

    for doc in docs:
        text = doc.text[:max_chars]
        for sub, _, note in chunk_document(doc, budget=budget):
            try:
                raw = predictor(document=sub.text).facts
            except Exception as exc:                  # noqa: BLE001 - LM failure should not crash the batch
                dropped.append({"field": "<document>",
                                "text": f"{doc.path.name} ({note}): {exc}"})
                continue
            ok, candidates, err = validate_candidates(raw)
            if not ok or not candidates:
                dropped.append({"field": "<document>",
                                "text": f"{doc.path.name} ({note}): {err or 'empty'}"})
                continue
            claims, lost = hydrate_claims(sub, candidates, now)
            for c in claims:
                key = (c.field, c.normalize())
                prev = seen.get(key)
                if prev is None:
                    seen[key] = c
                    all_claims.append(c)
                elif (len(c.provenance), c.confidence) > (len(prev.provenance),
                                                          prev.confidence):
                    c.provenance.extend(prev.provenance)
                    all_claims[all_claims.index(prev)] = c
                    seen[key] = c
            dropped.extend({**d, "text": f"[{doc.path.name} {note}] {d['text']}"}
                           for d in lost)
            if claims and doc.path.name not in used:
                used.append(doc.path.name)

    return ExtractionReport(claims=all_claims, dropped=dropped, documents=used)


# ---- JSON Resume direct structured extraction (no LLM) ----

_JR_SUMMARY = ("summary", "basics.summary")


def parse_json_resume_structured(doc: Document, now: str) -> list[Claim]:
    """JSON Resume -> Claim[] (without going through an LLM).

    JSON Resume is already structured; routing it through a model would only add uncertainty.
    confidence=0.9 (no model randomness, but still not human-confirmed — status remains DRAFT).
    """
    if doc.fmt is not DocFormat.JSON_RESUME:
        raise ValueError("parse_json_resume_structured expects a json_resume Document")
    raw = json.loads(doc.path.read_text(encoding="utf-8"))
    out: list[Claim] = []

    def add(field_name: str, text: str, pointer: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        out.append(Claim(
            field=field_name, text=text, status=ClaimStatus.DRAFT, confidence=0.9,
            provenance=[Provenance(doc.path.name, pointer, text[:400], now,
                                   extractor="json-resume-native")],
        ))

    basics = raw.get("basics", {}) or {}
    for key, f in (("name", "identity.full_name"), ("email", "identity.email"),
                   ("phone", "identity.phone"), ("summary", "identity.summary")):
        if isinstance(basics.get(key), str):
            add(f, basics[key], f"/basics/{key}")
    loc = basics.get("location") or {}
    if isinstance(loc, dict):
        city = " ".join(str(loc[k]) for k in ("city", "countryCode") if loc.get(k))
        add("identity.location", city.strip(), "/basics/location")
    # work_authorization: JSON Resume has no standard field; accept a few common aliases
    for key in ("workAuthorization", "work_authorization", "visaStatus"):
        if basics.get(key):
            add("identity.work_authorization", str(basics[key]), f"/basics/{key}")
            break

    for i, job in enumerate(raw.get("work", []) or []):
        if not isinstance(job, dict):
            continue
        add(f"experiences.{i}.org", job.get("name") or job.get("company") or "",
            f"/work/{i}/name")
        add(f"experiences.{i}.title", job.get("position") or "", f"/work/{i}/position")
        add(f"experiences.{i}.start", job.get("startDate") or "", f"/work/{i}/startDate")
        add(f"experiences.{i}.end", job.get("endDate") or "", f"/work/{i}/endDate")
        for j, h in enumerate(job.get("highlights", []) or []):
            add(f"experiences.{i}.bullets.{j}", h, f"/work/{i}/highlights/{j}")

    for i, edu in enumerate(raw.get("education", []) or []):
        if not isinstance(edu, dict):
            continue
        add(f"education.{i}.institution", edu.get("institution") or "",
            f"/education/{i}/institution")
        add(f"education.{i}.degree", edu.get("studyType") or edu.get("degree") or "",
            f"/education/{i}/studyType")

    for i, sk in enumerate(raw.get("skills", []) or []):
        if isinstance(sk, dict):
            add(f"skills.{i}", sk.get("name") or "", f"/skills/{i}/name")
            for j, kw in enumerate(sk.get("keywords", []) or []):
                add(f"skills.{i}.keywords.{j}", kw, f"/skills/{i}/keywords/{j}")
        elif isinstance(sk, str):
            add(f"skills.{i}", sk, f"/skills/{i}")

    return out
