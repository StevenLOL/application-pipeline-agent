"""F1 compliance check engine core: rule-table driven → per-rule execution → line-located violation report.

Three rule kinds (staged development plan, stage-2 spec) plus two condition-style ones inherited from _130:
  regex       regex hit (inherited from _130 FAIL_RULES; hard/warn recorded as-is)
  lambda      deterministic custom function (inherited from _130 CUSTOM)
  must_have   required statement missing (inherited from _130 MUST_HAVE; missing means hard)
  conditional condition-style: trigger present while satisfy absent (inherited from _130 ISO21434)
  corpus      corpus comparison (template-residue, added in stage 2; requires ctx.corpus)
  llm         LLM-assisted (claim-verification, enabled in stage 3; before a client is injected,
              raise an explicit error — silently skipping would be equivalent to the rule not existing)

Output: FileResult/Report (render_text aligns with the _130 readable format; to_dict feeds CI artifacts).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# rule kinds
KIND_REGEX = "regex"
KIND_LAMBDA = "lambda"
KIND_MUST_HAVE = "must_have"
KIND_CONDITIONAL = "conditional"
KIND_CORPUS = "corpus"
KIND_LLM = "llm"


@dataclass(frozen=True)
class Rule:
    id: str                                  # unique id, e.g. R11-1 / template-residue
    code: str                                # red-line code R1-R12, or accident rule name
    desc: str                                # human-readable description
    kind: str
    hard: bool = True                        # True=fail on hit; False=warning (manual review)
    pattern: str | None = None               # KIND_REGEX
    fn: Callable[[str], list[tuple[int, str]]] | None = None  # KIND_LAMBDA
    must: str | None = None                  # KIND_MUST_HAVE: regex that must appear
    trigger: str | None = None               # KIND_CONDITIONAL
    satisfy: str | None = None               # KIND_CONDITIONAL
    provenance: str = ""                     # provenance: _130 line number / accident number / design doc section


@dataclass
class Finding:
    rule_id: str
    code: str
    desc: str
    line: int          # 0 = document-level (missing/conditional), consistent with _130
    segment: str
    hard: bool

    def key(self) -> tuple:
        return (self.rule_id, self.code, self.desc, self.line, self.hard)


@dataclass
class FileResult:
    path: str
    n_chars: int = 0
    n_lines: int = 0
    missing: bool = False
    findings: list[Finding] = field(default_factory=list)

    @property
    def hard_count(self) -> int:
        return sum(1 for f in self.findings if f.hard)

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if not f.hard)


@dataclass
class Report:
    files: list[FileResult] = field(default_factory=list)

    def extend(self, fr: FileResult) -> None:
        self.files.append(fr)

    @property
    def hard_count(self) -> int:
        return sum(f.hard_count for f in self.files)

    @property
    def warn_count(self) -> int:
        return sum(f.warn_count for f in self.files)

    def to_dict(self) -> dict:
        return {
            "hard_count": self.hard_count,
            "warn_count": self.warn_count,
            "files": [
                {
                    "path": fr.path,
                    "n_chars": fr.n_chars,
                    "n_lines": fr.n_lines,
                    "missing": fr.missing,
                    "findings": [vars(f) for f in fr.findings],
                }
                for fr in self.files
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def render_text(self) -> str:
        """Readable format aligned with _130, for manual comparison."""
        out: list[str] = []
        for fr in self.files:
            out.append("=" * 66)
            if fr.missing:
                out.append(f"!! MISSING {fr.path}")
                continue
            out.append(f"{fr.path} ({fr.n_chars} chars, {fr.n_lines} lines)")
            for f in fr.findings:
                mark = "🔴" if f.hard else "⚠️ "
                if f.line == 0:
                    out.append(f"  {mark} [missing] [{f.code}] {f.desc}")
                else:
                    out.append(f"  {mark} [{f.code}] {f.desc}  L{f.line}: …{f.segment}…")
            if not fr.findings:
                out.append("  ✅ no hard issues found")
            out.append("")
        out.append("=" * 66)
        total = self.hard_count
        out.append("RESULT: " + ("✅ all passed" if total == 0 else f"🔴 {total} to fix"))
        return "\n".join(out)


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n") + 1


def check_text(text: str, rules: Sequence[Rule], ctx: "Context | None" = None) -> list[Finding]:
    """Run the rule table over a single text segment. ctx.corpus serves corpus-kind rules; ctx.llm serves llm-kind rules."""
    findings: list[Finding] = []
    for r in rules:
        if r.kind == KIND_REGEX:
            for m in re.finditer(r.pattern, text, re.I):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=_line_of(text, m.start()),
                    segment=text[max(0, m.start() - 50):m.end() + 50].replace("\n", " "),
                ))
        elif r.kind == KIND_LAMBDA:
            for line, seg in r.fn(text):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=line, segment=seg,
                ))
        elif r.kind == KIND_MUST_HAVE:
            if not re.search(r.must, text, re.I):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=0, segment=r.desc,
                ))
        elif r.kind == KIND_CONDITIONAL:
            if re.search(r.trigger, text, re.I) and not re.search(r.satisfy, text, re.I):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=0, segment=r.desc,
                ))
        elif r.kind == KIND_CORPUS:
            if ctx is None or not ctx.corpus:
                continue  # corpus rules do not apply without corpus context (caller should provide it explicitly)
            for line, seg in r.fn(text, ctx.corpus):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=line, segment=seg,
                ))
        elif r.kind == KIND_LLM:
            if ctx is None or ctx.llm is None:
                raise RuntimeError(
                    f"LLM rule {r.id} is registered but no LLM client was injected; "
                    "llm-kind rules should not be registered before stage 3 (see the staged development plan)"
                )
            for line, seg in r.fn(text, ctx.llm):
                findings.append(Finding(
                    rule_id=r.id, code=r.code, desc=r.desc, hard=r.hard,
                    line=line, segment=seg,
                ))
        else:  # pragma: no cover - defensive
            raise ValueError(f"unknown rule kind: {r.kind} ({r.id})")
    return findings


@dataclass
class Context:
    """Rule execution context: corpus = {filename: text}; llm = callable client (stage 3)."""
    corpus: dict[str, str] = field(default_factory=dict)
    llm: Callable | None = None
    target_path: str | None = None   # the file under inspection (corpus rules use it to identify "our fork")


def check_files(paths: Sequence[str | Path], rules: Sequence[Rule],
                ctx: "Context | None" = None) -> Report:
    report = Report()
    for p in paths:
        p = Path(p)
        fr = FileResult(path=str(p))
        if not p.is_file():
            fr.missing = True
            report.extend(fr)
            continue
        text = p.read_text(encoding="utf-8")
        fr.n_chars = len(text)
        fr.n_lines = len(text.splitlines())
        fr.findings = check_text(text, rules, ctx)
        report.extend(fr)
    return report
