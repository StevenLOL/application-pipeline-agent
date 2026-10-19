"""Review-comment auditor (stage 6+ / meta-governance): audits **third-party input** review comments.

Background: APA can both emit review comments (e.g. reviews of singapore assets) and must be able to audit
review comments given by others — otherwise "governance" applies only to itself and is one-directional.

What is audited is not whether an opinion is right or wrong, but **whether this comment can be adopted**,
i.e. whether it has: an evidence anchor, a reproducible path, an impact-tier argument, an executable
action, no boundary crossing, numeric self-consistency, and a source declaration.

Eight dimensions (all deterministic machine checks, no LLM, no network):
1. evidence    evidence anchor: no anchor = unverified (adopting it is a gamble)
2. repro       reproducibility: external-source citation only, with no re-runnable command = needs manual review
3. wording     phrasing: vague qualifiers used as conclusions / absolutes without support
4. grade       tier argument: whether there is an impact chain beside 🔴🟡🟢 (impact/causes/risk/depends/blocks)
5. action      actionability: whether action + subject + channel are all present
6. boundary    boundary crossing: whether it advises actions violating the iron rules (blocking level)
7. numeric     numeric self-consistency: the same keyword with different numbers and no declared basis
8. provenance  metadata: whether date/reviewer/target/source are declared

Anti-false-positive discipline (stage-3 lesson: the gate itself can also produce false positives):
- Items where the author has honestly flagged uncertainty (assumption / to-verify / unverified / basis /
  speculative) → the evidence dimension is **exempt** (honesty beats disguise; honesty is not punished)
- The auditor outputs "adoption advice" rather than verdicts; the human has the final say (isomorphic with HITL)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- evidence anchor patterns ----
_FILE_RE = re.compile(r"[\w一-鿿\-/.\\]+\.(?:py|md|json|ya?ml|txt|xlsx|csv|ps1|toml)\b", re.I)
_LINE_RE = re.compile(r"(?:L\d+|:?\d+\s*行|第\s*\d+\s*行|第\d+节|§\s*[\d.]+|line\s*\d+)", re.I)
_CMD_RE = re.compile(r"`[^`]*(?:apa\s+\w+|python\s+[\w./\\-]+\.py|pytest|git \w+|conda run)[^`]*`", re.I)
# The unit table covers high-frequency quantifiers in review docs (an "N items" omission once caused a
# measured misjudgment)
_NUM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|行|处|条|个|次|项|种|类|份|点|倍|天|周|月|年|"
    r"小时|分钟|秒|ms|s\b|"
    r"rows?|items?|occurrences?|cases?|entries?|records?|values?|hits?|"
    r"files?|packages?|roles?|jobs?|targets?|links?|tests?|runs?|metrics?|"
    r"points?|days?|weeks?|months?|years?|hours?|minutes?|seconds?)",
    re.I,
)
_QUOTE_RE = re.compile(r"[\"“”「『][^\"“”「『」』]{8,}[\"“”」』]")
_URL_RE = re.compile(r"https?://\S+")

EVIDENCE_RES = (_FILE_RE, _LINE_RE, _CMD_RE, _NUM_RE, _QUOTE_RE, _URL_RE)

# ---- wording ----
HEDGE_RE = re.compile(
    r"(?:可能|大概|大约|似乎|也许是|应该是|建议进一步|有待|不确定|不确定的是|估计|"
    r"\bmaybe\b|\bpossibly\b|\bprobably\b|\blikely\b|\bseems?\b|\bappears?\b|"
    r"\buncertain\b|\bunclear\b|\btbd\b)", re.I)
ABSOLUTE_RE = re.compile(
    r"(?:一定|必然|绝对|所有|全部|必须|毫无疑问|永远|"
    r"\ball\b|\bevery\b|\balways\b|\bnever\b|\babsolutely\b|"
    r"\bwithout exception\b|\bguaranteed\b|\bmust\b)", re.I)
HONEST_RE = re.compile(
    r"(?:假设|待核实|待确认|未验证|未证实|口径|推测|推测性|不确定|需人工|需复核|存疑|"
    r"\bassumed?\b|\bassumption\b|\bunverified\b|\bunconfirmed\b|"
    r"\bto[- ]be[- ]verified\b|\bto[- ]be[- ]confirmed\b|\bspeculative\b|"
    r"\buncertain\b|\bquestionable\b|\btbd\b|\bcalibration\b|"
    r"\bneeds? (?:human|manual) (?:review|check|reconciliation)\b)", re.I)

# ---- tier and argument ----
GRADE_RE = re.compile(r"(?:🔴|🟡|🟢|\[(?:高|中|低)(?:危|优先)?\]|\[(?:high|medium|low|critical)\]|P[012])", re.I)
IMPACT_RE = re.compile(
    r"(?:影响|导致|后果|依赖|风险|阻塞|阻断|失效|失败|无法|会|中断|损失|漏|"
    r"\baffect\w*\b|\bimpact\w*\b|\bcauses?\b|\bleads? to\b|\bresults? in\b|"
    r"\bdepends?\b|\bdependency\w*\b|\brisk\w*\b|\bblocks?\b|\bblocking\b|"
    r"\bbreak\w*\b|\bfails?\b|\bfailure\b|\bcannot\b|\boutage\b|"
    r"\blost\b|\bloss\b|\bmissed?\b|\bdowntime\b|\bunavailable\b)", re.I)

# ---- action ----
ACTION_RE = re.compile(
    r"(?:建议|应|需|请|应当|改为|修正|删除|补齐|更新|收口|迁移|复跑|验证|"
    r"\bsuggest\w*\b|\bshould\b|\bmust\b|\bneeds?\b|\bplease\b|\bfix\w*\b|"
    r"\bupdate\w*\b|\bremove\w*\b|\bdelete\w*\b|\badd\b|\bfill\w*\b|"
    r"\bchange\w*\b|\bmigrat\w*\b|\bre-?run\w*\b|\bverif\w*\b|\bcorrect\w*\b|"
    r"\bcomplet\w*\b|\brefactor\w*\b|\bcollapse\w*\b|\brewrite\w*\b)", re.I)
CHANNEL_RE = re.compile(r"(?:jd_registry_ops\.py|经\s*ops|唯一通道|pytest|CLI|apa \w+|脚本|命令|脚本化|\bops\b|\bcli\b|\bregistry\b|\bcron\b|\bscheduler\b|via\s+`?[\w\-./]+\.py)", re.I)

# ---- boundary crossing (advice violating the iron rules) ----
BOUNDARY_RE = re.compile(
    r"(?:直接改台账|手工改\s*xlsx|绕过\s*(?:jd_registry_ops|ops|唯一通道)|"
    r"shell\s*直传|直接\s*传中文|跳过\s*HITL|自动提交|删除\s*审计|关掉\s*审计|"
    r"禁用\s*规则|去掉\s*门禁|跳过\s*(?:测试|CI)|"
    r"directly\s+(?:edit\w*|modif\w+|chang\w+)\s+(?:the\s+)?ledger|"
    r"hand[- ]?edit\w*\s+(?:the\s+)?(?:ledger|xlsx)|"
    r"bypass\w*\s+(?:the\s+)?(?:jd_registry_ops|ops|only[- ]channel|gate|audit|hitl)|"
    r"pass\w*\s+chinese\s+(?:text\s+)?(?:directly\s+)?(?:via|through)\s+shell|"
    r"shell[- ]direct\w*\s+chinese|"
    r"skip\w*\s+(?:the\s+)?(?:hitl|tests?|ci)\b|"
    r"auto[- ]?submitt?\w*|automatically\s+submit\w*|"
    r"(?:delet|disabl|dropp)\w*\s+(?:the\s+)?audit|"
    r"(?:disabl|remov|dropp)\w*\s+(?:the\s+)?(?:rules?|gates?|guardrails?))",
    re.I,
)

# ---- author-declared evidence statements ("**evidence**: ..." / "basis:") — the content is argument, not a claim ----
EVIDENCE_LABEL_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)?\s*\*\*(?:证据|依据|实测|实证|evidence|basis|measured|verified)\*\*\s*[:：]", re.I)

# ---- claim-bearing vocabulary (whether a heading carries a claim) and negation contexts ----
CLAIM_RE = re.compile(
    r"(?:缺|缺少|缺失|建议|应|需|必须|问题|风险|错误|不一致|矛盾|缺陷|漏洞|不足|"
    r"\bmissing\b|\bproblem\w*\b|\brisk\w*\b|\berror\w*\b|\binconsisten\w*\b|"
    r"\bcontradict\w*\b|\bdefect\w*\b|\bflaw\w*\b|\binsufficient\b|"
    r"\bshould\b|\bmust\b|\bneeds?\b|\blacks?\b|\brefactor\w*\b)", re.I)
NEGATION_RE = re.compile(
    r"(?:禁|禁止|不要|不可|切勿|避免|不应|不许|不得|严禁|别)\s*$|"
    r"(?:禁|禁止|不要|不可|切勿|避免|不应|不许|不得|严禁|别|"
    r"\bban\w*\b|\bnever\b|\bdo\s+not\b|\bdon't\b|\bmust\s+not\b|"
    r"\bshall\s+not\b|\bforbidden\b|\bprohibit\w*\b|\bavoid\w*\b)", re.I)

# ---- metadata ----
PROVENANCE_RES = {
    "date": re.compile(r"(?:日期|生成时间|评审时间|\bdate\b|\bgenerated\b|\breviewed on\b)\s*[:：]?\s*\d{4}", re.I),
    "reviewer": re.compile(r"(?:评审人|作者|来源\s*人|\breviewer\b|\bauthor\b|\bby\s*[:：])", re.I),
    "review-target": re.compile(r"(?:评审对象|被审对象|评审\s*对象|针对|对象|移交文档|适用范围|\bsubject\b|\breview target\b|\bscope\b|\btarget\b)", re.I),
    "source": re.compile(r"(?:信源|来源|数据来源|依据|\bsources?\b|\bdata source\b|\bbasis\b)", re.I),
}
# display names for finding labels (tests assert on these exact strings)
_PROVENANCE_LABEL = {"date": "date", "reviewer": "Reviewer",
                     "review-target": "review-target", "source": "source"}


@dataclass
class Block:
    """One entry in a review document (heading section / list item / table row)."""

    kind: str  # heading | bullet | table
    text: str
    line: int
    grade: str | None = None
    section_evidence: bool = False  # heading: whether any entry inside its section carries an evidence anchor


@dataclass
class Finding:
    dimension: str
    severity: str  # high | medium | low
    line: int
    block: str
    note: str
    advice: str  # REJECT | EVIDENCE-REQUEST | REVIEW | ACCEPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "severity": self.severity,
            "line": self.line,
            "block": self.block,
            "note": self.note,
            "advice": self.advice,
        }


@dataclass
class AuditResult:
    path: str
    blocks: list[Block] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def by_dimension(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.dimension] = c.get(f.dimension, 0) + 1
        return c

    def verdict(self) -> str:
        """Overall adoption advice.

        Tier semantics (calibrated by measured runs): **only boundary-crossing advice is rejected** — an
        unsupported claim should trigger "supply evidence" rather than negating the whole comment;
        otherwise a high-quality review that has already been adopted could be killed by two judgment
        sentences.
        """
        if any(f.severity == "high" and f.dimension == "boundary" for f in self.findings):
            return "REJECT-OR-REVISE (boundary-crossing advice present: actions breaking iron rules should be rejected)"
        c = self.counts()
        if c.get("high", 0):
            return "ACCEPT-WITH-REQUESTS (unverified claims present: evidence must be supplied before adoption)"
        if c.get("medium", 0):
            return "ACCEPT-WITH-REQUESTS (acceptable, but evidence/actions/basis statements must be supplied)"
        return "ACCEPT (evidence, action, and impact chain all present)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "blocks": len(self.blocks),
            "findings": [f.to_dict() for f in self.findings],
            "counts": self.counts(),
            "by_dimension": self.by_dimension(),
            "verdict": self.verdict(),
            "meta": self.meta,
        }


def parse_blocks(text: str) -> list[Block]:
    """Split a review document into entries: heading sections / list items / table rows."""
    blocks: list[Block] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            blocks.append(Block("heading", stripped, i))
        elif stripped.startswith(("|",)):
            blocks.append(Block("table", stripped, i))
        elif stripped.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+\.\s", stripped):
            blocks.append(Block("bullet", stripped, i))
    for b in blocks:
        m = GRADE_RE.search(b.text)
        b.grade = m.group(0) if m else None
    return _annotate_section_evidence(blocks)


def _annotate_section_evidence(blocks: list[Block]) -> list[Block]:
    """Evidence inheritance for headings: if any entry inside a section carries an evidence anchor, that heading is considered supported.

    Anti-false-positive (two rounds of measured runs):
    - a heading like "### 1.3 content-level defects (2 items)" has no anchor itself but the body has evidence → eliminated via inheritance;
    - under `## III. Gaps and improvement suggestions` a `### G1` subheading follows immediately; if only the nearest heading
      were credited, the parent section would never get evidence → changed to **bubble up to all ancestors
      along the heading stack**.
    """
    stack: list[tuple[int, Block]] = []  # (level, heading)
    for b in blocks:
        if b.kind == "heading":
            level = len(b.text) - len(b.text.lstrip("#"))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, b))
            continue
        if stack and any(r.search(b.text) for r in EVIDENCE_RES):
            for _, h in stack:
                h.section_evidence = True
    return blocks


# ---- per-dimension checkers ----


def check_evidence(blocks: list[Block]) -> list[Finding]:
    """Evidence anchor: no anchor means unverified; honestly flagged uncertainty is exempt (anti-false-positive).

    Anti-false-positive (caught in a self-audit measured run): section headings ("## 1. 🔴 must-fix") and metadata
    sentences are inherently not claims; auditing them together would turn 8 of the 17 hits into noise —
    **a heading line is audited only when it contains claim-bearing vocabulary (missing/suggest/should/need/
    problem/risk/error/inconsistent)**.
    """
    out: list[Finding] = []
    for b in blocks:
        if b.kind == "table":  # the table itself is often an evidence carrier; skip
            continue
        if b.kind == "heading" and not CLAIM_RE.search(b.text):
            continue  # purely structural heading: not a claim
        if b.kind == "heading" and b.section_evidence:
            continue  # the section already has evidence → the heading is not required again
        if b.kind == "bullet" and not (CLAIM_RE.search(b.text) or ACTION_RE.search(b.text)):
            continue  # background/declaration sentences: carry no claim or action, so no evidence anchor required
        if len(b.text.strip()) < 12:  # short lines are not audited
            continue
        if any(r.search(b.text) for r in EVIDENCE_RES):
            continue
        if HONEST_RE.search(b.text):
            continue  # honestly flagged uncertainty → exempt
        # the author explicitly marked this as an evidence statement ("- **evidence**:..." / "basis:") — its content
        # is argument, not a claim, so it is not required to carry an anchor again (measured run: judging an
        # "evidence:" line as "no evidence" is absurd)
        if EVIDENCE_LABEL_RE.search(b.text):
            continue
        # this line has no anchor, but other entries in the same section do → downgrade to pending
        # confirmation (the evidence may be above; judging "unverified" is too heavy; measured run:
        # advice lines and evidence lines written on separate lines is a common pattern)
        if b.kind == "bullet" and _section_has_evidence(blocks, b):
            out.append(
                Finding(
                    "evidence", "medium", b.line, b.text[:60],
                    "this line has no evidence anchor, but other entries in the same section do — manually "
                    "confirm whether that evidence supports this entry",
                    "REVIEW",
                )
            )
            continue
        out.append(
            Finding(
                "evidence", "high", b.line, b.text[:60],
                "the entry has no evidence anchor (file/line number/command/measured number/quote/URL), and "
                "uncertainty is not flagged",
                "EVIDENCE-REQUEST",
            )
        )
    return out


def _section_has_evidence(blocks: list[Block], target: Block) -> bool:
    """Whether the section target belongs to (its nearest parent heading) contains an entry with evidence."""
    owner: Block | None = None
    for b in blocks:
        if b.line >= target.line:
            break
        if b.kind == "heading":
            owner = b  # the nearest (any-level) parent heading
    return bool(owner is not None and owner.section_evidence)


def check_reproducibility(blocks: list[Block]) -> list[Finding]:
    """Reproducibility: evidence exists but is external-URL citation only (no re-runnable command/local file) → needs manual review."""
    out: list[Finding] = []
    for b in blocks:
        if b.kind == "table" or not _URL_RE.search(b.text):
            continue
        if _CMD_RE.search(b.text) or _FILE_RE.search(b.text) or _LINE_RE.search(b.text):
            continue
        out.append(
            Finding(
                "repro", "medium", b.line, b.text[:60],
                "the evidence is an external-URL citation only, with no re-runnable command or local file "
                "anchor (unverifiable once the source goes dead)",
                "REVIEW",
            )
        )
    return out


def check_wording(blocks: list[Block]) -> list[Finding]:
    """Wording: vague qualifiers used as conclusions / absolute assertions without support."""
    out: list[Finding] = []
    for b in blocks:
        if b.kind == "table":
            continue
        hedge = HEDGE_RE.search(b.text)
        if hedge and not any(r.search(b.text) for r in (_NUM_RE, _FILE_RE, _CMD_RE)):
            out.append(
                Finding("wording", "medium", b.line, b.text[:60],
                        f'vague phrasing "{hedge.group(0)}" with no number/file support; should not be '
                        "stated as a conclusion",
                        "EVIDENCE-REQUEST")
            )
        abs_m = ABSOLUTE_RE.search(b.text)
        if abs_m and not any(r.search(b.text) for r in (_NUM_RE, _FILE_RE, _CMD_RE, _QUOTE_RE)):
            out.append(
                Finding("wording", "medium", b.line, b.text[:60],
                        f'absolute phrasing "{abs_m.group(0)}" without support '
                        "(no number/file/command/quote)",
                        "EVIDENCE-REQUEST")
            )
    return out


def check_grade(blocks: list[Block]) -> list[Finding]:
    """Tier argument: there must be an impact-chain explanation beside the 🔴🟡🟢/P0-2 marker."""
    out: list[Finding] = []
    for b in blocks:
        if not b.grade:
            continue
        window = b.text
        if not IMPACT_RE.search(window):
            out.append(
                Finding("grade", "medium", b.line, b.text[:60],
                        f"tier marker {b.grade} shows no impact-chain argument "
                        "(impact/causes/risk/depends/blocks)",
                        "REVIEW")
            )
    return out


def check_actionability(blocks: list[Block]) -> list[Finding]:
    """Actionability: advice-type entries should have an action + channel (who does it via which channel)."""
    out: list[Finding] = []
    for b in blocks:
        if b.kind == "table":
            continue
        if not ACTION_RE.search(b.text):
            continue
        if CHANNEL_RE.search(b.text) or _CMD_RE.search(b.text) or _FILE_RE.search(b.text):
            continue
        out.append(
            Finding("action", "medium", b.line, b.text[:60],
                    "an action is given but no execution channel/carrier is specified (who, via which "
                    "command or script)",
                    "REVIEW")
        )
    return out


def check_boundary(blocks: list[Block], text: str) -> list[Finding]:
    """Boundary crossing: advising actions that violate the iron rules → blocking level. Whole-document scan (not per entry).

    Anti-false-positive (caught in a self-audit measured run): **negation-context exemption** — "forbid
    shell-direct Chinese transfer" and "bypassing ops is forbidden" are **prohibitions** rather than advice; flagging them amounts to punishing the
    rule-abiding (isomorphic with the negation-context false positive of the stage-2 A1 rule).
    """
    out: list[Finding] = []
    for i, raw in enumerate(text.splitlines(), 1):
        m = BOUNDARY_RE.search(raw)
        if not m:
            continue
        head = raw[: m.start()]
        if NEGATION_RE.search(head):
            continue  # negation context: a prohibition, not advice
        out.append(
            Finding("boundary", "high", i, raw.strip()[:60],
                    f"suspected advice to violate an iron rule: \"{m.group(0)}\""
                    " (only-channel / no shell-direct Chinese transfer / never bypass HITL / never delete "
                    "audit records)",
                    "REJECT")
        )
    return out


def check_numeric_consistency(text: str) -> list[Finding]:
    """Numeric self-consistency: the same keyword is given different numbers before and after, with no declared basis difference."""
    out: list[Finding] = []
    seen: dict[str, list[tuple[str, int]]] = {}
    declared = re.search(
        r"(?:口径|差异|修正|原记|实为|实测|此前记|\bcalibration\b|\bdifference\b|"
        r"\bdiscrepancy\b|\bcorrected\b|\brevised\b|\bpreviously\b|\bmeasured\b|\bbasis\b)",
        text, re.I)
    pat = re.compile(
        r"([\w一-鿿]{2,10})\s*[:：]?\s*(\d+(?:\.\d+)?)\s*"
        r"(行|处|条|个|次|%|rows?|items?|occurrences?|cases?|entries?|records?|values?|hits?)",
        re.I)
    for i, raw in enumerate(text.splitlines(), 1):
        for m in pat.finditer(raw):
            key, num, unit = m.group(1), m.group(2), m.group(3)
            seen.setdefault(f"{key}{unit}", []).append((num, i))
    for key, vals in seen.items():
        nums = {n for n, _ in vals}
        if len(nums) > 1 and not declared:
            lines = ", ".join(f"{n}@L{i}" for n, i in vals)
            out.append(
                Finding(
                    "numeric", "medium", vals[0][1], key,
                    f"the same metric shows different numbers ({lines}), and no "
                    "(basis/difference/correction) declaration appears in the document",
                    "REVIEW",
                )
            )
    return out


def check_provenance(text: str) -> list[Finding]:
    """Metadata: whether the review date/reviewer/review target/source are declared."""
    out: list[Finding] = []
    for name, rx in PROVENANCE_RES.items():
        if not rx.search(text):
            out.append(
                Finding("provenance", "medium", 1, f"missing '{_PROVENANCE_LABEL[name]}' declaration",
                        "review comments should declare their source and target, otherwise their scope of "
                        "applicability and currency cannot be judged",
                        "EVIDENCE-REQUEST")
            )
    return out


def audit_file(path: str | Path) -> AuditResult:
    """Audit one review-comment document."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    blocks = parse_blocks(text)
    res = AuditResult(path=str(p), blocks=blocks)
    res.findings.extend(check_boundary(blocks, text))
    res.findings.extend(check_evidence(blocks))
    res.findings.extend(check_reproducibility(blocks))
    res.findings.extend(check_wording(blocks))
    res.findings.extend(check_grade(blocks))
    res.findings.extend(check_actionability(blocks))
    res.findings.extend(check_numeric_consistency(text))
    res.findings.extend(check_provenance(text))
    res.findings.sort(key=lambda f: (f.severity != "high", f.line))
    res.meta = {
        "blocks": len(blocks),
        "graded_blocks": sum(1 for b in blocks if b.grade),
        "chars": len(text),
    }
    return res


def render_markdown(res: AuditResult) -> str:
    """Audit report (markdown)."""
    lines = [
        f"# Review Comment Audit Report: {Path(res.path).name}",
        "",
        f"- blocks: {res.meta.get('blocks', 0)} (with grade markers "
        f"{res.meta.get('graded_blocks', 0)})",
        f"- verdict: **{res.verdict()}**",
        f"- findings: {res.counts() or '{}'}",
        f"- by dimension: {res.by_dimension() or '{}'}",
        "",
        "## findings",
        "",
        "| severity | dimension | line | block | note | advice |",
        "|---|---|---|---|---|---|",
    ]
    for f in res.findings:
        blk = f.block.replace("|", "\\|")[:50]
        note = f.note.replace("|", "\\|")
        lines.append(f"| {f.severity} | {f.dimension} | L{f.line} | {blk} | {note} | {f.advice} |")
    if not res.findings:
        lines.append("| — | — | — | no findings | — | ACCEPT |")
    lines += [
        "",
        "## dimension reference (auditing adoptability, not whether the "
        "opinion is right)",
        "",
        "| dimension | rule | typical advice |",
        "|---|---|---|",
        "| evidence | no evidence anchor and no declared uncertainty | EVIDENCE-REQUEST |",
        "| repro | external URL only, no reproducible command | REVIEW |",
        "| wording | vague/absolute phrasing without support | EVIDENCE-REQUEST |",
        "| grade | grade without impact-chain argument | REVIEW |",
        "| action | action without an execution channel | REVIEW |",
        "| boundary | advises breaking an iron rule | REJECT |",
        "| numeric | same metric, different numbers, no declared basis | REVIEW |",
        "| provenance | missing source/target/date declaration | EVIDENCE-REQUEST |",
        "",
        "> False-positive discipline: blocks where the author honestly "
        "declared (assumption / to-verify / basis / "
        "unverified) are exempt on the evidence dimension — honesty beats "
        "disguise; the auditor never punishes honesty. Output is advice; "
        "the human decides.",
        "",
        "## Conclusion",
        "",
        f"- verdict: **{res.verdict()}**",
        "- next: findings above are adoption advice, not verdicts — the human "
        "decides what to adopt.",
    ]
    return "\n".join(lines)
