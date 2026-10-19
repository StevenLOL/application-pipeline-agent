"""R0 document parser: docx / pdf / txt / md / json_resume -> unified Document (docs/19 §4.2 ①).

**Pure functions, zero LLM, zero network** — parsing is deterministic; extraction is
the model's job. This separation makes the parsing layer fully unit-testable, and
lets "a failed extraction be retried without re-parsing".

Per-format locator semantics (see ``models.Provenance``):
- txt/md: ``chars a-b`` (whole-text character range, directly highlightable)
- docx: ``para N`` (python-docx paragraph index, 0-based; tables are additionally
  marked ``table N row M``)
- pdf: ``page N`` (1-based)
- json_resume: ``/work/0/highlights/1`` (JSON pointer)

**Scanned PDFs are explicitly unsupported** (docs/19 §9-5): pypdf only extracts the
text layer; image-only PDFs yield empty text — in that case **raise and say so**,
never pretend success (a fake success writes an empty profile, which is harder to
notice than a failure).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import DocFormat


class UnsupportedDocument(ValueError):
    """Unsupported format / scanned PDF — the error message must be actionable (docs/19 §6 F4)."""


@dataclass
class Segment:
    """One locatable fragment of a document.

    ``_start``/``_end`` are whole-text character ranges (internal indices, computed
    by ``extract._attach_segments``, never persisted) — the UI uses them to highlight
    the snippet back into the source, and providers use them to swap in segment-level
    locators.
    """

    text: str
    locator: str
    _start: int = 0
    _end: int = 0


@dataclass
class Document:
    """The parsed, unified document view."""

    path: Path
    fmt: DocFormat
    text: str
    segments: list[Segment] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)   # text blocks converted out of docx tables
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)


def detect_format(path: Path) -> DocFormat:
    """Decide the format by file suffix (unknown suffix -> UNSUPPORTED; the error message lists what is supported)."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "docx":
        return DocFormat.DOCX
    if suffix == "pdf":
        return DocFormat.PDF
    if suffix in ("txt", "text", "rtf"):
        return DocFormat.TXT
    if suffix in ("md", "markdown"):
        return DocFormat.MD
    if suffix == "json":
        return DocFormat.JSON_RESUME
    return DocFormat.UNSUPPORTED


SUPPORTED_HINT = "supported: .docx .pdf .txt .md .json (case-insensitive)"


# ---- txt / md ----

_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1")


def _decode(raw: bytes) -> tuple[str, str]:
    """Encoding sniffing (returns (text, encoding that matched)).

    The order is fixed: utf-8-sig -> utf-8 -> utf-16 -> cp1252 -> latin-1 (latin-1
    never fails and is the last-resort fallback; GBK/traditional-Chinese user
    documents decode into mojibake at cp1252 but **do not crash** — the review
    console relies on snippet highlighting to make that visible to a human).
    """
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def parse_txt(path: Path) -> Document:
    raw = path.read_bytes()
    text, enc = _decode(raw)
    segments = [Segment(text=line.strip(), locator=f"chars {i}-{i + len(line)}")
                for i, line in enumerate(text.splitlines()) if line.strip()]
    return Document(path=path, fmt=DocFormat.TXT, text=text, segments=segments,
                    meta={"encoding": enc})


_MD_NOISE = re.compile(r"^\s*(?:```|~~~|\||-{3,}\s*$|>|\*\*\d{4}\*\*\s*$)")


def parse_md(path: Path) -> Document:
    """Markdown: keep structural markers (heading levels / list bullets), drop code
    fences and table-alignment noise.

    No full CommonMark parsing — resume md comes in only a few shapes (headings +
    lists + tables), and over-parsing loses information. Code block contents are
    **kept** (users' technical project documents often put quantified metrics in
    code comments).
    """
    doc = parse_txt(path)
    kept: list[str] = []
    for line in doc.text.splitlines():
        if re.match(r"^\s*```", line):
            kept.append(line)          # fence lines kept as location anchors; content collected as usual
            continue
        kept.append(line)
    text = "\n".join(kept)
    segments = [Segment(text=l.strip(), locator=f"chars {i}-{i + len(l)}")
                for i, l in enumerate(kept) if l.strip() and not _MD_NOISE.match(l)]
    return Document(path=path, fmt=DocFormat.MD, text=text, segments=segments,
                    meta=doc.meta)


# ---- docx ----

def parse_docx(path: Path) -> Document:
    """python-docx: paragraphs + tables.

    Tables are converted into separate text blocks and added to ``tables`` (the
    "skills matrix" table in a resume is often the hardest part for ATS to read;
    docs/19 §9-4: **no dedicated table parser** — convert wholesale to text and
    hand it to the LLM; add one only if real-world use shows problems). Paragraphs
    whose style is List Bullet/List Number serve as bullet cues.
    """
    try:
        import docx  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment missing the dependency
        raise UnsupportedDocument(
            "reading .docx needs python-docx: pip install python-docx") from exc

    d = docx.Document(str(path))
    lines: list[str] = []
    segments: list[Segment] = []
    tables: list[str] = []
    for i, para in enumerate(d.paragraphs):
        t = para.text.strip()
        if not t:
            continue
        style = (para.style.name or "") if para.style is not None else ""
        if style.lower().startswith(("list", "bullet")):
            t = f"- {t}"
        lines.append(t)
        segments.append(Segment(text=t, locator=f"para {i}"))

    for ti, table in enumerate(d.tables):
        rows: list[str] = []
        for ri, row in enumerate(table.rows):
            cells = [c.text.strip() for c in row.cells]
            rows.append(" | ".join(cells))
            for ci, cell in enumerate(cells):
                if cell:
                    segments.append(Segment(text=cell,
                                           locator=f"table {ti} row {ri} col {ci}"))
        blob = "\n".join(rows)
        tables.append(blob)
        lines.append(blob)

    text = "\n".join(lines)
    if not text.strip():
        raise UnsupportedDocument(
            f"{path.name}: no text extracted (is it a scanned/image .docx?)")
    return Document(path=path, fmt=DocFormat.DOCX, text=text, segments=segments,
                    tables=tables, meta={"paragraphs": str(len(d.paragraphs))})


# ---- pdf ----

def parse_pdf(path: Path) -> Document:
    """pypdf: per-page text_extraction, locator = ``page N`` (1-based).

    Scanned files (no text layer) **raise explicitly** — see the module docstring.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedDocument(
            "reading .pdf needs pypdf: pip install pypdf") from exc

    reader = PdfReader(str(path))
    segments: list[Segment] = []
    lines: list[str] = []
    for pno, page in enumerate(reader.pages, start=1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception:                          # noqa: BLE001 - individual pages can raise
            t = ""
        if not t:
            continue
        lines.append(t)
        segments.append(Segment(text=t, locator=f"page {pno}"))

    text = "\n".join(lines)
    if not text.strip():
        raise UnsupportedDocument(
            f"{path.name}: no text layer — this looks like a scanned PDF. "
            "APA does not run OCR; export a text-based PDF or supply .docx/.md")
    return Document(path=path, fmt=DocFormat.PDF, text=text, segments=segments,
                    meta={"pages": str(len(reader.pages))})


# ---- json resume (standard format ingested directly, no extraction) ----

def parse_json_resume(path: Path) -> Document:
    """JSON Resume (v1.x): parsed into a text view that runs through the same
    extraction pipeline, but JSON pointers are preserved.

    Keeping the pointers means even "re-importing the same resume.json" can do
    multi-source corroboration — the symmetric side of docs/19 §4.4 "do not invent
    a fifth schema of our own": it can go in, and it can come back out.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UnsupportedDocument(f"{path.name}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise UnsupportedDocument(f"{path.name}: expected a JSON object at top level")

    segments: list[Segment] = []

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{pointer}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{pointer}/{i}")
        elif isinstance(node, str) and node.strip():
            segments.append(Segment(text=node.strip(), locator=pointer))

    walk(data, "")
    if not segments:
        raise UnsupportedDocument(f"{path.name}: no string values found")
    text = "\n".join(s.text for s in segments)
    return Document(path=path, fmt=DocFormat.JSON_RESUME, text=text, segments=segments,
                    meta={"keys": ",".join(sorted(data.keys()))})


_PARSERS = {
    DocFormat.TXT: parse_txt,
    DocFormat.MD: parse_md,
    DocFormat.DOCX: parse_docx,
    DocFormat.PDF: parse_pdf,
    DocFormat.JSON_RESUME: parse_json_resume,
}


def parse_document(path: Path) -> Document:
    """Dispatch a parser by file suffix. Unknown format -> UnsupportedDocument (with the supported list)."""
    fmt = detect_format(path)
    if fmt is DocFormat.UNSUPPORTED or fmt not in _PARSERS:
        raise UnsupportedDocument(
            f"cannot ingest {path.name!r} ({path.suffix or 'no extension'}); {SUPPORTED_HINT}")
    return _PARSERS[fmt](path)


def parse_dir(directory: Path, recursive: bool = True) -> tuple[list[Document], list[tuple[Path, str]]]:
    """Parse a directory in batch. Returns (successful documents, [(failed path, reason)]).

    A single file failing **does not abort the batch** — when a user drops 5
    documents at once, 1 scanned file should not waste the other 4; failures are
    returned as-is for the CLI/UI to display (docs/19 §6 F4).
    """
    docs: list[Document] = []
    failures: list[tuple[Path, str]] = []
    pattern = "**/*" if recursive else "*"
    for p in sorted(directory.glob(pattern)):
        if not p.is_file() or p.name.startswith("."):
            continue
        try:
            docs.append(parse_document(p))
        except (UnsupportedDocument, OSError) as exc:
            failures.append((p, str(exc)))
    return docs, failures
