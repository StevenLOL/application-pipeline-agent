"""F1 rule set, part 2: accident-driven rules (incident log section 2.2, four quality accidents).

Stage-2 scope (staged development plan):
  template-residue   accident 1 "ACME data-domain phrase residue entering an Fabrikam letter" — cross-fork contamination
  number-provenance  accident 3 "seven procurement packages cannot be reconciled item by item" — numbers without provenance
Stage-3 scope (staged development plan, 2026-10-17):
  cross-block-dupe       accident 2 "the same letter repeats a claim across 4 blocks" — the same claim restated across blocks
  claim-verification     accident 4 "unverified checklist items treated as action instructions" — action-item entities lack a JD basis
  cl-resume-consistency  the planning doc's original fifth rule — CL vs. master caliber consistency (revival of deprecated phrasings)

All five are **warning level (hard=False)**: the semantics are "surfacing for manual review", not machine
adjudication — the cost of a false positive is one visual confirmation, the cost of a miss is another
3 rounds of revision going unnoticed (staged development plan, section 8: false-positive fatigue discipline).

LLM timing-tension handling (the staged development plan's stage-3 requirement that claim-verification be
"LLM-assisted"):
the local model only lands in stage 8 (llama.cpp + Qwen GGUF), so all five stage-3 rules are implemented with a
**deterministic core**: claim-verification v0 = deterministic traceability of action-item entities against the
JD source text; semantic-level claim matching (caliber claims like "scores submitted with the application") is left to the LLM slot,
injected in stage 8 via KIND_LLM
(the engine already supports this: an explicit RuntimeError when no client is injected, never silently skipped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..engine import KIND_CORPUS, KIND_LAMBDA, Rule

# ── template-residue (accident 1) ────────────────────────────────────────

# fork is derived from the CL filename: CoverLetter_<FORK>_....md
_FORK_RE = re.compile(r"^CoverLetter_([A-Za-z]+)_")
# shingle length range (words): start at 4 words; shorter word groups carry a high generalization risk;
# 8+ words fully cover residue segments
_SHINGLE_NS = (4, 5, 6, 7, 8)

# Structural boilerplate stopwords (v0, added after the stage-2 first-run measurements): dates/titles/contact
# info/sign-offs etc. — format differences across forks create false "exclusive content". A span with ≥60%
# structural tokens is discarded — measured to cut false positives on a clean Helios letter from 14 down to
# about 8; the remainder are genuinely verbatim shared segments (manually confirmed).
_STRUCTURAL_TOKENS = frozenset("""
january february march april may june july august september october november december
hiring manager re dear yours faithfully steven du singapore citizen
senior assistant director manager engineer lead head officer specialist scientist architect
programme program role position job id ref ltd co inc
gmail com github www http https tel phone email
""".split())


def _structural_ratio(tokens: list[str]) -> float:
    if not tokens:
        return 1.0
    hit = sum(1 for t in tokens if t in _STRUCTURAL_TOKENS or t.isdigit())
    return hit / len(tokens)


@dataclass
class Corpus:
    """corpus = all same-family CLs except the target file ({filename: text}); target_fork = the target fork."""
    others: dict[str, str]
    target_fork: str | None


def fork_of(filename: str) -> str | None:
    m = _FORK_RE.match(filename)
    return m.group(1) if m else None


def _tokens(text: str) -> list[tuple[str, int]]:
    """Lowercase tokens + original-text offsets (used for line numbers and segment location)."""
    return [(m.group(), m.start()) for m in re.finditer(r"[a-z0-9]+", text.lower())]


def _build_exclusive_shingles(others: dict[str, str]) -> dict[tuple[str, ...], str]:
    """Word shingles appearing in only a single fork's CL → fork name.

    Shingles shared by multiple forks (genuine achievements reused across letters, e.g. "ten percentage points")
    are naturally excluded — this is exactly the distinction from "residue": residue is another fork's exclusive content.
    """
    shingle_forks: dict[tuple[str, ...], set[str]] = {}
    for name, text in others.items():
        fork = fork_of(name)
        if not fork:
            continue
        toks = [w for w, _ in _tokens(text)]
        for n in _SHINGLE_NS:
            for i in range(len(toks) - n + 1):
                shingle_forks.setdefault(tuple(toks[i:i + n]), set()).add(fork)
    return {s: next(iter(fs)) for s, fs in shingle_forks.items() if len(fs) == 1}


def template_residue_check(text: str, corpus: Corpus) -> list[tuple[int, str]]:
    """Exclusive shingles of other forks in the target text (merged into segments) → (line number, segment note)."""
    if corpus.target_fork is None:
        return []  # cross-fork semantics do not apply to non-CL files (e.g. resumes)
    exclusive = _build_exclusive_shingles(corpus.others)
    if not exclusive:
        return []

    toks = _tokens(text)
    words = [w for w, _ in toks]
    hits: list[tuple[int, int, str]] = []  # (start tok, end tok, fork)
    for n in _SHINGLE_NS:
        for i in range(len(words) - n + 1):
            sh = tuple(words[i:i + n])
            fork = exclusive.get(sh)
            if fork and fork != corpus.target_fork:
                hits.append((i, i + n - 1, fork))
    if not hits:
        return []

    # merge overlapping/adjacent hits into maximal segments
    hits.sort()
    merged: list[list] = [[hits[0][0], hits[0][1], hits[0][2]]]
    for s, e, f in hits[1:]:
        if s <= merged[-1][1] + 1 and f == merged[-1][2]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, f])

    # example source files for that fork (for manual traceability)
    fork_files: dict[str, list[str]] = {}
    for name in corpus.others:
        f = fork_of(name)
        if f:
            fork_files.setdefault(f, []).append(name)

    out: list[tuple[int, str]] = []
    for s, e, fork in merged:
        span_tokens = [w for w, _ in toks[s:e + 1]]
        if _structural_ratio(span_tokens) >= 0.6:
            continue  # structural boilerplate (header/title/sign-off/date), not residue
        pos_s = toks[s][1]
        pos_e = toks[e][1] + len(toks[e][0])
        line = text[:pos_s].count("\n") + 1
        seg = re.sub(r"\s+", " ", text[pos_s:pos_e])[:90]
        src = fork_files.get(fork, ["?"])[0]
        out.append((line, f"[{fork} exclusive content] …{seg}… (same text seen in {src}; please confirm this is intentional)"))
    return out


# ── number-provenance (accident 3) ───────────────────────────────────────

# quantity words: integers / English number words / compound number words (forty-two / twenty-one)
_NUM = (r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
        r"thirty|forty|fifty|sixty|seventy|eighty|ninety)"
        r"(?:[-–— ](?:one|two|three|four|five|six|seven|eight|nine))?")
_UNITS = (r"(?:percentage\s+points?|percent|%|tests?|projects?|packages?|documents?|patents?|"
          r"people|persons?|members?|models?|commits?|repositor(?:y|ies)|roles?|teams?|"
          r"children|applications?)")
# Up to ≤2 modifier words are allowed between NUM and UNIT (granted / procurement etc.) — the accident-3
# original sentences "seven procurement packages" and "sixteen granted patents" both carry modifiers;
# tight matching would miss them
_QUANTITY_RE = re.compile(rf"\b({_NUM})\s+(?:[a-z]+\s+){{0,2}}?({_UNITS})\b", re.I)
_RATIO_RE = re.compile(r"\b\d+\s*/\s*\d+\b")

# Quantity expressions with registered provenance (registration key = normalized NUM+UNIT, modifiers
# excluded) — only verifiable ones are collected; "seven packages" (the accident-3 case itself) is
# deliberately absent — lacking provenance means it must be flagged.
# Expanded after the stage-3 false-positive evaluation (run_001): each entry below has a local
# authoritative source, verified item by item by actual runs/reads.
_PROVENANCE_RAW: dict[str, str] = {
    "eleven years": "resume master me/简历.md (working since 2011; R11 final wording: eleven years in technology)",
    "ten percentage points": "resume master (Zephyr speech-recognition improvement, mass-production volume product)",
    "seven person": "resume master (7-person AI team from scratch)",
    "sixteen patents": "me/StevenDu_Patents_Awards_Publications.md (16 granted, employer-assigned)",
    "two children": "confirmed by the user themselves (family info, CL header)",
    "four applications": "git evidence (4 of the 7 projects since 2026-08 are application-type)",
    "59/63": "singapore run table (reference corpus bm25 recall@5 59/63 = 93.7%)",
    "39/63": "singapore run table (same table, top-1 39/63)",
    "31/31": "singapore run table (scale corpus hybrid recall@5 31/31, consistent with the Makefile record of 2026-09-30)",
    "37 document": "singapore run table (scale corpus 37 documents / 1052 chunks)",
    "165 tests": "actual run of the ragdemo suite on 2026-10-17: 165 items total (164 passed + 1 skipped; "
                 "test_assets.py:133 lacks the real asset pack tome-1.7.6-gfx, an environmental skip); the run log records 165 passed",
    "61 tests": "singapore CI record (ragdemo CI 61 tests — point-in-time count; now 165, see the entry above)",
    # "seven packages" **deliberately not registered**: the accident-3 case itself. The fixed state (41dcad0)
    # changed the bare number to an in-sentence itemized enumeration (CAG letter "for seven packages:
    # voice/vision/ToF/info-sec/two labs"), where the enumeration is self-evidencing within the sentence —
    # but the registry is number-level and cannot see the contextual enumeration.
    # If registered, the accident-3 gold-standard negative (the git dirty-version bare number) would no
    # longer be flagged, and the regression anchor would be lost.
    # Handling: keep it unregistered; hits on the CAG letter are confirmed manually (warning-level contract:
    # surfacing, not adjudication); context-level self-evidence recognition is left to the stage-8 LLM slot.
    "three patents": "me/StevenDu_Patents_Awards_Publications.md #2 CN116704621A in-vehicle liveness prompt / "
                     "#8 CN111144404A leftover-object detection / #12 CN112489466A traffic-light recognition (all granted; "
                     "stated as three granted vision patents in the Litware Vision/Woodgrove letter)",
    "two roles": "resume master employment history (the two most recent roles = Evergrande + Zephyr; ZTE is earlier)",
    "1 model": "me/StevenDu_Patents_Awards_Publications.md (1 of the 16 is a utility model, CN211166535U)",
}

# Number words → Arabic numerals (for registration-key normalization): "ten percentage points" and
# "10 percentage points" are two spellings of the same quantity (verified in the stage-3 CLI first run:
# JD threshold judgments use numerals, CLs use words; without normalization, registered quantities
# would be flagged repeatedly).
_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}


def _canon_num(num: str) -> str:
    """Quantity-word normalization: pure digits pass through; number words (including compounds like forty-two) become Arabic numerals."""
    n = num.lower().strip()
    if n.isdigit():
        return n
    parts = re.split(r"[-–— ]+", n)
    if all(p in _NUM_WORDS for p in parts) and parts:
        total = sum(_NUM_WORDS[p] for p in parts)
        # a compound must be "tens+ones" (40+2) or a single word; twenty + one = 21 is also valid
        if len(parts) == 1 or (len(parts) == 2 and _NUM_WORDS[parts[0]] >= 20
                               and _NUM_WORDS[parts[0]] % 10 == 0
                               and 1 <= _NUM_WORDS[parts[1]] <= 9):
            return str(total)
    return n


def _canon_key(key: str) -> str:
    """Registration-key normalization: a leading quantity word becomes a numeral (eleven years → 11 years)."""
    parts = key.split(" ", 1)
    if len(parts) == 2:
        return f"{_canon_num(parts[0])} {parts[1]}"
    return key


PROVENANCE: dict[str, str] = {_canon_key(k): v for k, v in _PROVENANCE_RAW.items()}


def _claim_key(num: str, unit: str) -> str:
    """Registration key = NUM + head noun (normalized: number words → numerals, modifiers excluded)."""
    unit = re.sub(r"\s+", " ", unit.lower())
    if unit.startswith("percentage"):
        unit = "percentage points"
    return f"{_canon_num(num)} {unit}"


def number_provenance_check(text: str) -> list[tuple[int, str]]:
    """Quantity expressions without registered provenance → (line number, segment note). Registered ones are not flagged again.

    Known boundaries (v0, documented): covers NUM (+≤2 modifiers)+UNIT and ratio forms; bare-word quantities
    with no unit noun ("a team of seven") are not covered yet — this rule is a surfacing layer, not an
    oracle; misses are backstopped by manual final review (warning-level positioning).
    """
    out: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for m in _QUANTITY_RE.finditer(text):
        key = _claim_key(m.group(1), m.group(2))
        if key in PROVENANCE:
            continue
        line = text[:m.start()].count("\n") + 1
        if (line, key) in seen:
            continue
        seen.add((line, key))
        out.append((line, f"[number without provenance] {m.group()} — needs manual reconciliation or registered provenance"))
    for m in _RATIO_RE.finditer(text):
        key = m.group().replace(" ", "")
        if key in PROVENANCE:
            continue
        line = text[:m.start()].count("\n") + 1
        if (line, key) in seen:
            continue
        seen.add((line, key))
        out.append((line, f"[number without provenance] {m.group()} — needs manual reconciliation or registered provenance"))
    return out


# ── cross-block-dupe (accident 2) ────────────────────────────────────────

# Accident-2 shape (incident log section 2.2 "the same letter repeats a claim across 4 blocks" + the
# third-party review source text review_3rd_party_raw.txt L93-111): within one letter, the same claim is
# stated once in multiple blocks —
#   C1 independent-work framing ×2 (second block "Since 2023 I have worked independently" +
#      closing "Between roles I have been building independently")
#   C2 workflow description ×2, nearly verbatim ("high-risk actions pause for operator approval")
#   C3 seven-person team ×2 (policy block ≈ second block)
# Why 3 rounds of revision missed it: each block reads fine on its own; only cross-block comparison exposes it.
#
# v0 has two deterministic detectors (both warning level):
#   (a) Claim anchors: each anchor = a claim family that actually repeated in the accident (regex + provenance).
#       The same anchor hitting ≥2 content blocks → warning. Under the "rules come from accidents" principle,
#       v0 only includes families with accident provenance; generalization (semantic-level claim alignment)
#       is left to the LLM slot (stage 8).
#   (b) Verbatim fragments: a ≥6-word shingle appearing in ≥2 content blocks → warning (catches unknown-form
#       verbatim repetition). Quotes (『…』/"…") are stripped first — threshold-judgment blocks intentionally
#       quote JD sentences; that is not repetition.
# Structural blocks (letterhead/contact info/salutation/Re line/date/signature/markdown headings) do not
# participate in the judgment — boilerplate repetition shared across letters is format, not a defect.

_STRUCTURAL_BLOCK_RE = re.compile(
    r"^#"                                        # markdown heading
    r"|@|https?://|\+\d{2}[\d -]{6,}"            # email / URL / phone (letterhead and sign-off)
    r"|^\s*(?:Dear\b|Yours\b|Sincerely\b)"       # salutation / sign-off
    r"|^\s*\*\*Re:"                              # subject line
    r"|^\s*\d{1,2}\s+[A-Z][a-z]+\s+20\d{2}\s*$"  # date line
    r"|Hiring Manager|Recruitment|Talent Acquisition",
    re.M,
)
_QUOTE_SPAN_RE = re.compile(r"『[^』]*』|「[^」]*」|\"[^\"]*\"")
_SHINGLE_N = 6  # shingle length: "repeats" under 6 words carry a high generalization risk in English technical letters

# Claim anchor table: family name → (regex, provenance). The word list is the rule's knowledge base —
# only accident-verified families are collected.
_CLAIM_ANCHORS: dict[str, tuple[str, str]] = {
    "independent-work": (
        r"\b(?:worked|works|work|working|been building|building|build)\s+independently\b",
        "review C1 (review_3rd_party_raw.txt L96-100): the independent-work framing is stated once in each of two blocks",
    ),
    "agentic-governance": (
        r"high[- ]risk actions?\s+(?:pause|paused|pausing)\s+for\s+(?:operator\s+)?approval",
        "review C2 (L101-103): the workflow description is repeated nearly verbatim across blocks",
    ),
    "team-size": (
        r"\b(?:seven|\d+)[- ](?:person|member|man)\s+(?:AI\s+)?team\b"
        r"|\bteam of seven\b",
        "review C3 (L104): the seven-person team is repeated across blocks",
    ),
}


def _content_blocks(text: str) -> list[tuple[int, str]]:
    """Split into blocks by blank lines (exact line numbers), filtering structural blocks → [(start line number, block text)]."""
    blocks: list[tuple[int, str]] = []
    cur: list[str] = []
    start = 0
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not cur:
                start = i
            cur.append(line)
        else:
            if cur:
                blocks.append((start, "\n".join(cur)))
                cur = []
    if cur:
        blocks.append((start, "\n".join(cur)))
    return [(ln, b) for ln, b in blocks if not _STRUCTURAL_BLOCK_RE.search(b)]


def _shingles(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def cross_block_dupe_check(text: str) -> list[tuple[int, str]]:
    """Cross-block duplication → (line number, segment note). v0 covers the C1/C2/C3 families + verbatim repetition."""
    blocks = _content_blocks(text)
    if len(blocks) < 2:
        return []
    out: list[tuple[int, str]] = []

    # (a) claim anchors: same family hitting ≥2 distinct content blocks
    for name, (pat, src) in _CLAIM_ANCHORS.items():
        hits = [(ln, b) for ln, b in blocks if re.search(pat, b, re.I)]
        if len(hits) >= 2:
            locs = ", ".join(f"L{ln}" for ln, _ in hits)
            out.append((hits[0][0],
                        f"[cross-block dupe · {name}] the same claim is stated once in each of {len(hits)} blocks ({locs})"
                        f" — {src}; merge into one place or make the division explicit"))

    # (b) verbatim fragments: after stripping quotes, a ≥6-word shingle shared across blocks
    stripped_blocks = [(ln, _QUOTE_SPAN_RE.sub(" ", b)) for ln, b in blocks]
    shingle_locs: dict[tuple[str, ...], list[int]] = {}
    for ln, b in stripped_blocks:
        toks = [w for w, _ in _tokens(b)]
        for sh in _shingles(toks, _SHINGLE_N):
            shingle_locs.setdefault(sh, []).append(ln)
    dupes = {sh: sorted(set(lns)) for sh, lns in shingle_locs.items() if len(set(lns)) >= 2}
    if dupes:
        # report each block pair once, represented by the shingle with the most shared words (the longest)
        pair_best: dict[tuple[int, int], tuple[str, int]] = {}
        for sh, lns in dupes.items():
            for a in range(len(lns)):
                for c in range(a + 1, len(lns)):
                    key = (lns[a], lns[c])
                    seg = " ".join(sh)
                    if key not in pair_best or len(seg) > pair_best[key][1]:
                        pair_best[key] = (seg, len(seg))
        for (la, lb), (seg, _) in sorted(pair_best.items()):
            out.append((la,
                        f"[cross-block dupe · verbatim] the {_SHINGLE_N}+-word fragment '…{seg}…' appears in both "
                        f"L{la} and L{lb} — verbatim cross-block repetition, please merge"))

    return out


# ── claim-verification (accident 4) ──────────────────────────────────────

# Accident-4 shape (incident log section 2.2 "unverified checklist items treated as action instructions"):
# package 22's (Fabrikam/Helios) JD.md delivery checklist contains "⚠️ Book and pass the CivicTech Digital
# Academy online technical assessment, with scores submitted with the application", but the JD source text mentions Digital
# Academy 0 times — the action item bled in from the hardcoded template string at
# _144_build_package.py:175 (that template was drafted for package 21's Contoso, and the Contoso JD does have this
# item: a natural control group, see evals/golden/).
# The _178 NOTE corrected form: "CivicTech Digital Academy assessment to be booked separately per the JD checklist".
#
# v0 deterministic core: the **employer process entities** (capitalized proper-noun phrases) in checklist
# action items must be traceable in the same document's JD source text, otherwise warn. Semantic-level claims
# (caliber matching for "scores submitted with the application") are left to the stage-8 LLM slot. Internal process items
# (using files in this directory / writing back to the ledger / recording submission times) are
# naturally excluded via code-span stripping + the Chinese no-context case, so no false positives.
#
# Bidirectional normalization before matching (accident-4 positive-control evidence, pkg21 JD:74): the Contoso
# JD source text writes the full name "Civic Technology Agency's Digital Academy", while the checklist
# writes the abbreviation "CivicTech Digital Academy" — two spellings of the same entity. Without
# normalization, the positive control would be falsely reported as unsupported. The normalization trio:
# official-abbreviation expansion + possessive stripping + curly-quote unification.
_ABBREV_EXPAND = {"civictech": "civic technology agency"}


def _norm_for_match(s: str) -> str:
    """Bidirectional entity/JD normalization: curly quotes → straight quotes, possessive stripping, whitespace
    folding, known official-abbreviation expansion (the registry grows from evidence, never invented)."""
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"'s\b", "", s)
    s = re.sub(r"\s+", " ", s).lower().strip()
    for abbr, full in _ABBREV_EXPAND.items():
        s = re.sub(rf"\b{abbr}\b", full, s)
    return s

_CHECKLIST_HEADER_RE = re.compile(
    r"^##\s*(?:投递\s*Checklist|Submission\s+Checklist|Application\s+Checklist|"
    r"Delivery\s+Checklist|Checklist)\s*$", re.M)
_JD_BODY_HEADER_RE = re.compile(
    r"^##\s*(?:JD\s*原文|Original\s+JD\s+Text|JD\s+Text\s*\((?:Original)\)|"
    r"Original\s+JD|JD\s+Text)\s*$", re.M)
_CHECKLIST_ITEM_RE = re.compile(r"^\s*-\s*\[[ xX]?\]\s*(.+)$", re.M)
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_MD_EMPH_RE = re.compile(r"[*_]{1,2}")
# Entity tokens: CamelCase-friendly (CivicTech/LinkedIn), possessives allowed
_ENTITY_TOKEN = r"[A-Z][A-Za-z]*(?:'[A-Z][a-z]+)?"
# Entity phrases: 2–5 consecutive capitalized tokens ("CivicTech Digital Academy")
_ENTITY_PHRASE_RE = re.compile(rf"\b(?:{_ENTITY_TOKEN}\s+){{1,4}}{_ENTITY_TOKEN}\b")
# Extended form allowing internal lowercase connectives (of/and/for/the/&) —
# long proper names like "Ministry of Digital Development and Information"
_ENTITY_PHRASE_LOOSE_RE = re.compile(
    rf"\b{_ENTITY_TOKEN}(?:\s+(?:{_ENTITY_TOKEN}|of|and|for|the|&)){{1,5}}\s+{_ENTITY_TOKEN}\b"
)


def _section(text: str, header_re: re.Pattern) -> tuple[str, int] | None:
    """Extract the markdown section text (from that header up to the next ##) → (section text, start offset).

    Returns the offset instead of back-computing with text.find() — if the section text appears again
    elsewhere in the document (e.g. the checklist content being quoted), find would locate the wrong position.
    """
    m = header_re.search(text)
    if not m:
        return None
    start = m.end()
    rest = text[start:]
    nxt = re.search(r"^##\s", rest, re.M)
    end = start + nxt.start() if nxt else len(text)
    return text[start:end], start


def _extract_entities(item: str) -> list[str]:
    """Extract employer process entities from a checklist line (after stripping code spans and emphasis marks)."""
    cleaned = _MD_EMPH_RE.sub("", _CODE_SPAN_RE.sub(" ", item))
    found: list[str] = []
    for rx in (_ENTITY_PHRASE_LOOSE_RE, _ENTITY_PHRASE_RE):
        for m in rx.finditer(cleaned):
            ent = re.sub(r"\s+", " ", m.group()).strip()
            # trim leftover fragments ending in a connective ("... and")
            ent = re.sub(r"\s+(?:of|and|for|the|&)$", "", ent)
            if len(ent) >= 6 and ent not in found:
                found.append(ent)
    return found


def claim_verification_check(text: str) -> list[tuple[int, str]]:
    """Checklist action-item entities with no basis in the JD source text → (line number, segment note).

    Non-JD.md (no two sections) → not applicable, returns []. Entity matching is case-insensitive and
    whitespace-normalized; pkg21 (Contoso, whose JD does have Digital Academy) is the natural positive control.
    """
    got = _section(text, _CHECKLIST_HEADER_RE)
    jd = _section(text, _JD_BODY_HEADER_RE)
    if got is None or jd is None:
        return []
    checklist, cl_start = got
    jd_norm = _norm_for_match(jd[0])
    out: list[tuple[int, str]] = []
    for m in _CHECKLIST_ITEM_RE.finditer(checklist):
        item = m.group(1)
        line = text[: cl_start + m.start()].count("\n") + 1
        for ent in _extract_entities(item):
            if _norm_for_match(ent) not in jd_norm:
                out.append((line,
                            f"[action item lacks JD basis] \"{ent}\" appears in the delivery checklist but the JD source text "
                            f"has no such entity (accident-4 shape) — trace it back to the JD or remove the action item"))
    return out


# ── cl-resume-consistency (the planning doc's original fifth rule) ───────

# Provenance: one of the five P0 F1 rules in the incident log; the planning doc section 5 "original
# planning-doc rule". Real accident basis: the 2026-10-16 "tenure caliber B" decision — stop reporting
# numeric tenure (both 11 years / 15 years easily conflict with the 2011 start timeline; HR review against
# the earliest year would strike it), unify on the "since 2011" phrasing; master + 13 CL sources + 4
# generators + package-21 historical files = a 42-file sweep of the whole repo (decision log, 2026-10-16).
# The master me/简历.md L11 carries its own deprecated-phrasings list — this rule's registry treats it as
# the authoritative source.
#
# v0 = deprecated-phrasing revival detection: a CL/resume showing a phrasing the master has deprecated →
# warning (document inconsistent with the master's caliber). General entity-level alignment (CL employer
# names/role names vs the master) is left to v0.1 (needs the master path context, see the stage-3 report
# backlog).
#
# ⚠️ Known divergence from the inherited rules (documented; the inherited rules are not modified): _130
# Phase 1 (finalized 10-13) rule R11-must-years still **requires** "eleven years in technology", while
# caliber B (10-16 04:05) has deprecated that phrasing — the inherited set is frozen at its finalized
# version; this rule reflects the current caliber.

DEPRECATED_PHRASINGS: dict[str, str] = {
    r"\b(?:eleven|11|fifteen|15)\s+years?\s+in\s+(?:technology|AI|AI/ML)\b":
        "deprecated phrasing in the master me/简历.md L11 (caliber B, 2026-10-16): stop reporting numeric tenure, "
        "unify on \"in AI since 2011\" / \"whose work has been in AI since 2011\"",
    r"\b8\+\s*(?:years?\s*)?in\s+AI/?ML\b|\beight\s+in\s+AI/?ML\b":
        "old deprecated items listed in master L11 (\"8+ in AI/ML\" / \"eight in AI/ML\")",
    r"\bMEng\s*\(?\s*CE\b|\bMEng\s+in\s+CE\b|\bMaster of Engineering in Computer Engineering\b":
        "certificate caliber (2026-10-16, aligned across 22 packages): do not write the subject name, "
        "only Master of Engineering from NBU",
    r"Evergrande New Energy Vehicle":
        "the user's revised judgment (2026-10-16 d0ac5b7): do not write the legal entity name, "
        "superseding decision 139",
    r"Bilingual English\s*/\s*Mandarin":
        "three-deletion decision (2026-10-16 d0ac5b7): the line removed from 10 CLs + the master",
}


def cl_resume_consistency_check(text: str) -> list[tuple[int, str]]:
    """A phrasing the master has deprecated revived in this document → (line number, segment note).

    Block-quote lines (starting with "> ") are skipped — the master's L11/L13 deprecated-phrasings list and
    allowed-phrasings list are **self-documentation of deprecated phrasings** (quotation), not document claims
    (run_001 false-positive evidence: all 4 hits came from master quote lines). Note: this quoting behavior
    itself exposes an internal contradiction in the master (L11 deprecates vs L13 still lists as allowed),
    recorded as a user decision item in the stage-3 report; this rule does not fix it on the master's behalf.
    """
    out: list[tuple[int, str]] = []
    for pat, src in DEPRECATED_PHRASINGS.items():
        for m in re.finditer(pat, text, re.I):
            line_no = text[:m.start()].count("\n") + 1
            line_text = text.splitlines()[line_no - 1]
            if line_text.lstrip().startswith(">"):
                continue  # block quote = editorial note/quotation, not a document claim
            out.append((line_no,
                        f"[inconsistent with master caliber] '{m.group()}' is a phrasing deprecated by the master — {src}"))
    return out


def accident_rules() -> list[Rule]:
    return [
        Rule(
            id="template-residue", code="A1", desc="cross-fork residue (accident 1: ACME data-domain phrases entering an Fabrikam letter)",
            kind=KIND_CORPUS, hard=False, fn=template_residue_check,
            provenance="incident log section 2.2, accident 1; staged development plan, stage 2",
        ),
        Rule(
            id="cross-block-dupe", code="A2", desc="cross-block duplication (accident 2: the same letter repeats a claim across 4 blocks)",
            kind=KIND_LAMBDA, hard=False, fn=cross_block_dupe_check,
            provenance="incident log section 2.2, accident 2; review_3rd_party_raw.txt L93-111; staged development plan, stage 3",
        ),
        Rule(
            id="number-provenance", code="A3", desc="quantity expression without provenance (accident 3: seven procurement packages)",
            kind=KIND_LAMBDA, hard=False, fn=number_provenance_check,
            provenance="incident log section 2.2, accident 3; staged development plan, stage 2",
        ),
        Rule(
            id="claim-verification", code="A4", desc="checklist action-item entities lack a JD source-text basis (accident 4: Digital Academy cross-contamination)",
            kind=KIND_LAMBDA, hard=False, fn=claim_verification_check,
            provenance="incident log section 2.2, accident 4; _144_build_package.py:175; _178 NOTE; staged development plan, stage 3",
        ),
        Rule(
            id="cl-resume-consistency", code="A5",
            desc="CL inconsistent with the master caliber (original planning-doc rule; revival of tenure caliber B deprecated phrasings)",
            kind=KIND_LAMBDA, hard=False, fn=cl_resume_consistency_check,
            provenance="incident log P0 F1; planning doc section 5; me/简历.md L11; decision log 2026-10-16; staged development plan, stage 3",
        ),
    ]
