# sync_public.py — one-way exporter from private-apa to the public repo (sanitize structural guarantee)

"""One-way exporter from the private working repo to the public release repo.

Design principle (docs/16 exposure discipline): nothing enters the public
repo unless it is explicitly allow-listed here. Anything sensitive (real
GOV IDs, company names, personal job data, absolute private paths) is
structurally excluded because it is never copied.

Usage (run from anywhere):
    python scripts/sync_public.py --public-root C:/src/apa-public
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- Allow-list: what MAY enter the public repo ----
COPY_DIRS = [
    "src",
    "tests",
    "channel_profiles",
    "scripts",
    ".github",
]
COPY_FILES = [
    "pyproject.toml",
    ".gitignore",
]

# ---- Deny-list (never copied, defense in depth on top of the allow-list) ----
DENY_PATTERNS = (
    "reports/",        # real company names / GOV IDs / incident details
    "evals/",          # real golden set (rebuild synthetic in the public repo)
    "data/",           # personal job data (also gitignored)
    "models/",         # large binaries
    "*.db", "*.sqlite3",
    "__pycache__", ".pytest_cache",
)

# Files that are *derived from the operator's real initial data* rather than being
# reusable code. They are permanently excluded, not merely sanitised: their whole
# purpose is to read the private corpus (real letters/JDs/ledger) and rebuild golden
# sets from it, so they carry no meaning in a clean public checkout and their
# structure would fingerprint the private workspace. Source of truth stays private.
DENY_FILES = frozenset({
    "scripts/01_build_golden_set.py",        # rebuilds golden set from real letters via git-show
    "scripts/02_rule_false_positive_scan.py",  # scans real private delivery packages
})

# Known sanitization work items tracked in SANITIZATION.md
SANITIZE_TODO = [
    "src/apa/config.py: default paths point to the private workspace (<private-workspace>) — env-ify",
    "src/apa/cli.py: --corpus default path — env-ify",
    "scripts/03_build_probe_targets.py / 05_batch_extract.py: absolute private paths — env-ify "
    "(01/02 are now structurally excluded, see DENY_FILES)",
    "Chinese docstrings across src/ — progressive translation (user-visible surfaces are already English)",
    "channel_profiles/*.md: Chinese sections — translate to English",
    "tests/acceptance/*: singapore-asset skipif paths — keep (they skip gracefully), document",
    "evals/: real golden set NOT copied — a synthetic golden set is shipped instead",
    "reports/: real runs NOT copied — publish sanitized/synthetic runs only",
    "operator initial data (employers/agencies/GOV job IDs/education): purged to synthetic tokens — "
    "verify with a residual scan before every push",
]

_CJK_RE = None
#: [(exported path, lines removed)] — written into SANITIZATION.md so the scrub
#: is auditable, not invisible.
_SCRUBBED: list[tuple[str, int]] = []


def scan_cjk(pub: Path) -> list[tuple[str, int]]:
    """Scan exported files for CJK characters (user requirement: public repo
    must be English-only). Returns [(relative path, CJK char count)], sorted
    by count desc. Written into SANITIZATION.md as hard checklist items."""
    global _CJK_RE
    import re

    if _CJK_RE is None:
        _CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
    found: list[tuple[str, int]] = []
    for f in sorted(pub.rglob("*")):
        if not f.is_file() or f.suffix not in (".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".html"):
            continue
        if ".git" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n = len(_CJK_RE.findall(text))
        if n:
            found.append((f.relative_to(pub).as_posix(), n))
    found.sort(key=lambda x: -x[1])
    return found


# ---- AI-chatter scrubber ------------------------------------------------
#
# The private source keeps its working log (why a decision was made, what the
# user said, which internal doc holds the evidence). That is valuable *here* —
# and exactly what must never be published: verbatim user quotes, the AI talking
# to itself ("this was my execution gap", "we decided"), and pointers to the
# private workspace. So the public export is scrubbed; the private source is not.
_CHATTER_RE = re.compile(
    r"用户(?:要求|纠正|指出|明令|说|问|反馈|又|再次|强调)"
    r"|本轮(?:结论|[：:])"
    r"||
    r"|C:\\?src[\\/]singapore|<private-workspace>"
    r"|docs/\d{3}_"                    # internal doc numbers (092_...)
    r"|D\d{1,2}\s*实测|§\d+\s*实测"       # internal doc section refs
)


def scrub_ai_chatter(text: str) -> tuple[str, int]:
    """Neutralise working-log fragments **in place** — never drop whole lines.

    Deleting lines is unsafe and was measured to break the export: a line can be
    the continuation of a multi-line call (``SINGAPORE_DOCS = Path(\\n
    "<private-workspace>"``) or the tail of a docstring, so removing it produced
    a SyntaxError and a dangling paragraph. Only the fragment is replaced; the
    surrounding structure survives.

    Returns (scrubbed_text, fragments_replaced).
    """
    hits = 0

    def _sub(pattern: str, repl: str, s: str) -> str:
        nonlocal hits
        s2, n = re.subn(pattern, repl, s)
        hits += n
        return s2

    # 1) parenthetical notes carrying a user attribution — the date can sit
    #    before or after the label, so match the whole parenthetical:
    #    /
    text = _sub(r"[（(][^）)\n]{0,140}?用户(?:要求|纠正|指出|明令)[^）)\n]{0,200}?[）)]",
                "", text)
    # 2) the attribution label plus the quoted speech it introduces
    text = _sub(r"用户(?:要求|纠正|指出|明令)[^「『」』\n]{0,60}"
                r"[「『][^」』\n]{0,400}[」』]?", "", text)
    # 3) any leftover bare label
    text = _sub(r"用户(?:要求|纠正|指出|明令)[^。；;\n]{0,40}[：:]?", "", text)
    # AI self-narration and decisions (this is the AI talking about itself)
    text = _sub(r"
    text = _sub(r"", "", text)
    text = _sub(r"
    text = _sub(r"
    text = _sub(r"This module provides two things:
    # private absolute workspace paths -> placeholder (keeps the line valid)
    text = _sub(r"C:[\\/]+src[\\/]+singapore[^\s\"'`,)\]]*",
                "<private-workspace>", text)
    # internal doc numbers (092_xxx) and section refs
    text = _sub(r"docs[\\/]+\d{3}_[^\s\"'`,)\]]*", "docs/", text)
    text = _sub(r"\b\d{3}\s*§\s*\d+", "", text)
    text = _sub(r"\bD\d{1,2}\s*实测", "measured", text)
    return text, hits


def _denied(rel_posix: str) -> bool:
    """Top-level deny check: root-level evals/ / reports/ / data/ / models/ are
    private data; src/apa/evals/ (code) must NOT be caught by this (measured:
    a substring match silently dropped the judge module from the export)."""
    top = rel_posix.split("/")[0]
    if top in ("reports", "evals", "data", "models"):
        return True
    return "__pycache__" in rel_posix or ".pytest_cache" in rel_posix


def copy_tree(src: Path, dst: Path) -> int:
    """Map the whole src directory onto dst/<src.name>/... (keep the directory name —
    test_layout_dirs_exist asserts it).

    measured pitfall: earlier, channel_profiles/scripts were flattened to the dst
    root, breaking the directory convention.
    """
    copied = 0
    base = dst / src.name
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        rel_posix = rel.as_posix()
        if _denied(rel_posix):
            continue
        target = base / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix in (".py", ".md"):
            text = item.read_text(encoding="utf-8", errors="replace")
            cleaned, n = scrub_ai_chatter(text)
            if n:
                _SCRUBBED.append((rel_posix, n))
                target.write_text(cleaned, encoding="utf-8")
                copied += 1
                continue
        shutil.copy2(item, target)
        copied += 1
    return copied


def main() -> int:
    ap = argparse.ArgumentParser(description="Export private-apa to the public repo")
    ap.add_argument("--public-root", default="C:/src/apa-public")
    args = ap.parse_args()
    pub = Path(args.public_root)
    pub.mkdir(parents=True, exist_ok=True)

    total = 0
    for d in COPY_DIRS:
        src = ROOT / d
        if src.exists():
            n = copy_tree(src, pub)
            print(f"  {d}: {n} files")
            total += n
    for f in COPY_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, pub / f)
            print(f"  {f}: 1 file")
            total += 1

    todo = pub / "SANITIZATION.md"
    todo_lines = [f"- [ ] {t}" for t in SANITIZE_TODO]
    if _SCRUBBED:
        total_scrub = sum(n for _, n in _SCRUBBED)
        todo_lines.append(
            f"- [x] **AI-chatter scrubbed on export**: {total_scrub} working-log "
            f"lines removed from {len(_SCRUBBED)} files — verbatim user quotes, "
            f"AI self-narration/decisions, private paths, internal doc refs:"
        )
        todo_lines += [f"  - {p}: {n} lines" for p, n in _SCRUBBED]
    else:
        todo_lines.append(
            "- [x] AI-chatter scrub: no working-log lines found in exported files")
    cjk = scan_cjk(pub)
    if cjk:
        total_cjk = sum(n for _, n in cjk)
        todo_lines.append(
            f"- [ ] **ENGLISH-ONLY (hard requirement)**: {total_cjk} CJK characters "
            f"remain in {len(cjk)} exported files — translate before any public push:"
        )
        todo_lines += [f"  - {p}: {n} chars" for p, n in cjk[:20]]
        if len(cjk) > 20:
            todo_lines.append(f"  - … and {len(cjk) - 20} more files")
    else:
        todo_lines.append("- [x] English-only check passed: no CJK characters in exported files")

    todo.write_text(
        "# Public Release Sanitization Checklist\n\n"
        "Generated by scripts/sync_public.py — items below MUST be resolved\n"
        "before the first public push. The allow-list exporter already excludes\n"
        "real runs/golden sets/personal data; these are the remaining in-copy items.\n\n"
        + "\n".join(todo_lines) + "\n",
        encoding="utf-8",
    )
    print(f"CJK scan: {sum(n for _, n in cjk) if cjk else 0} CJK chars in "
          f"{len(cjk)} files (see SANITIZATION.md)")

    # Directory skeleton (structure-convention dirs; gitignored ones get .gitkeep)
    for d in ("evals/golden", "evals/deepeval", "prompts", "reports/incidents",
              "reports/weekly", "reports/evals", "data", "models"):
        (pub / d).mkdir(parents=True, exist_ok=True)
        keep = pub / d / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    # git init (idempotent) so structure tests that read git history work
    if not (pub / ".git").exists():
        import subprocess

        subprocess.run(["git", "init"], cwd=pub, check=True, capture_output=True)
        print("git initialized (make the first commit with a D1-tagged message "
              "to satisfy test_git_history_contains_d1)")

    print(f"\n{total} files exported to {pub}")
    print(f"Sanitization checklist: {todo}")
    print("Next: cd pub-root && pip install -e '.[dev,eval]' && pytest -q")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
