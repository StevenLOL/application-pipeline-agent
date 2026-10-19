"""R0 文档解析器：docx / pdf / txt / md / json_resume → 统一 Document（docs/19 §4.2 ①）。

**纯函数、零 LLM、零网络**——解析是确定性的，抽取才是模型的事。这个分离让
解析层可完整单测，也让「抽取失败可重试而不必重解析」。

各格式的 locator 语义（见 ``models.Provenance``）：
- txt/md：``chars a-b``（全文字符区间，可直接高亮）
- docx：``para N``（python-docx 段落序号，0-based；表格另标 ``table N row M``）
- pdf：``page N``（1-based）
- json_resume：``/work/0/highlights/1``（JSON 指针）

**扫描件 PDF 明确不支持**（docs/19 §9-5）：pypdf 只抽文本层，图片型 PDF 会
产出空文本——此时**抛错并说清**，不假装成功（假成功会写出空 profile，
比失败更难发现）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import DocFormat


class UnsupportedDocument(ValueError):
    """不支持的格式 / 扫描件 PDF——错误消息必须可操作（docs/19 §6 F4）。"""


@dataclass
class Segment:
    """文档的一个可定位片段。

    ``_start``/``_end`` 是全文字符区间（内部索引，由 ``extract._attach_segments``
    计算，不落盘）——UI 用它把 snippet 高亮回原文，provider 用它换成段级 locator。
    """

    text: str
    locator: str
    _start: int = 0
    _end: int = 0


@dataclass
class Document:
    """解析后的统一文档视图。"""

    path: Path
    fmt: DocFormat
    text: str
    segments: list[Segment] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)   # docx 表格转出的文本块
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)


def detect_format(path: Path) -> DocFormat:
    """按后缀判定格式（未知后缀 → UNSUPPORTED，错误消息给出支持清单）。"""
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
    """编码嗅探（返回 (文本, 命中的编码)）。

    顺序固定：utf-8-sig → utf-8 → utf-16 → cp1252 → latin-1（latin-1 永不失败，
    是最后的兜底；用户文档里 GBK/繁体中文会在 cp1252 处解出乱码但**不崩**，
    审阅台靠 snippet 高亮让人看出来）。
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
    """Markdown：保留结构标记（标题层级/列表符），去掉代码围栏与表格对齐噪声。

    不做完整 CommonMark 解析——简历 md 的形态就那几种（标题+列表+表格），
    过度解析反而丢信息。代码块内容**保留**（用户的技术项目文档常把量化指标
    写在代码注释里）。
    """
    doc = parse_txt(path)
    kept: list[str] = []
    for line in doc.text.splitlines():
        if re.match(r"^\s*```", line):
            kept.append(line)          # 围栏行保留为定位锚点，内容照常收
            continue
        kept.append(line)
    text = "\n".join(kept)
    segments = [Segment(text=l.strip(), locator=f"chars {i}-{i + len(l)}")
                for i, l in enumerate(kept) if l.strip() and not _MD_NOISE.match(l)]
    return Document(path=path, fmt=DocFormat.MD, text=text, segments=segments,
                    meta=doc.meta)


# ---- docx ----

def parse_docx(path: Path) -> Document:
    """python-docx：段落 + 表格。

    表格单独转文本块并进 ``tables``（简历里的「技能矩阵表」常是 ATS 最难读的
    部分，docs/19 §9-4：**不写专用表格解析器**，整体转文本交 LLM，实测出问题
    再加）。段落里样式为 List Bullet/List Number 的作为 bullet 线索。
    """
    try:
        import docx  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境缺依赖
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
    """pypdf：逐页 text_extraction，locator = ``page N``（1-based）。

    扫描件（无文本层）**明确报错**——见模块 docstring。
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
        except Exception:                          # noqa: BLE001 - 个别页会抛
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


# ---- json resume（标准格式直入，免抽取） ----

def parse_json_resume(path: Path) -> Document:
    """JSON Resume（v1.x）：解析成文本视图走同一条抽取管线，但保留 JSON 指针。

    保留指针让「同一份 resume.json 二次导入」也能做多来源印证——这是 docs/19
    §4.4「不自己发明第五个 schema」的对称面：既进得去，也出得来。
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
    """按后缀分派解析器。未知格式 → UnsupportedDocument（含支持清单）。"""
    fmt = detect_format(path)
    if fmt is DocFormat.UNSUPPORTED or fmt not in _PARSERS:
        raise UnsupportedDocument(
            f"cannot ingest {path.name!r} ({path.suffix or 'no extension'}); {SUPPORTED_HINT}")
    return _PARSERS[fmt](path)


def parse_dir(directory: Path, recursive: bool = True) -> tuple[list[Document], list[tuple[Path, str]]]:
    """批量解析目录。返回 (成功文档, [(失败路径, 原因)])。

    单个文件失败**不中断整批**——用户一次丢 5 份文档，1 份是扫描件不该让
    其余 4 份白跑；失败项原样返回给 CLI/UI 展示（docs/19 §6 F4）。
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
