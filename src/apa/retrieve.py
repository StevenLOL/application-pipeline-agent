"""F5 retrieval stack v0 (R1): local embeddings + faiss vector search.

Purpose (usability first): a new JD comes in -> retrieve the most similar
historical submission packages (reuse your own scores / verdicts / lessons) so
the agent does not cold-start from zero. This is "RAG over your own history".

D10 version: single-route dense (bge-family) + faiss IndexFlatIP. BM25/RRF/rerank
and the ablation (dense/BM25/hybrid/+rerank four conditions, Ragas + bootstrap
CI) land at the R1 ablation milestone - get a usable baseline up first.

Design:
- ``Embedder`` protocol: encode(texts) -> normalized vectors (inner product = cosine).
  Implementations: ``BgeEmbedder`` (sentence-transformers local model, bge-small-en-v1.5,
  model storage per rule 12); ``FakeEmbedder`` (deterministic hash vectors - offline unit tests).
- ``faiss.IndexFlatIP``: after normalization, inner product is cosine (same conclusion as rag-gov-demo).
- Index persistence: faiss.write_index + docs.json (doc_id / path / metadata).
- Discipline: retrieval is a **read-only** capability - index building reads the
  corpus and writes no business data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


# ---- Embedder protocol and implementations ----

class Embedder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


class FakeEmbedder:
    """Deterministic hash vectors (256-dim, L2-normalized) - offline unit tests; semantically meaningless but reproducible."""

    DIM = 256

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            for token in re.findall(r"[a-z]{3,}", t.lower()):
                vecs[i, hash(token) % self.DIM] += 1.0
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    @property
    def model_name(self) -> str:
        return "fake-hash-256"


class BgeEmbedder:
    """Local bge embedding (sentence-transformers; model files stored per rule 12)."""

    def __init__(self, model_path: str | Path):
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(str(model_path))

    def encode(self, texts: list[str]) -> np.ndarray:
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)

    @property
    def model_name(self) -> str:
        return str(self._model)


# ---- Index and retrieval ----

@dataclass
class RetrievedDoc:
    doc_id: str
    source_path: str
    score: float
    preview: str
    meta: dict[str, Any] = field(default_factory=dict)


class Retriever:
    """faiss IndexFlatIP + document metadata (docs.json) persistent retriever."""

    def __init__(self, embedder: Embedder, index_dir: str | Path):
        self.embedder = embedder
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._index = None
        self._docs: list[dict[str, Any]] = []
        self._load()

    # -- persistence --

    @property
    def index_file(self) -> Path:
        return self.index_dir / "corpus.faiss"

    @property
    def docs_file(self) -> Path:
        return self.index_dir / "docs.json"

    def _load(self) -> None:
        if self.index_file.exists() and self.docs_file.exists():
            import faiss

            self._index = faiss.read_index(str(self.index_file))
            self._docs = json.loads(self.docs_file.read_text(encoding="utf-8"))

    # -- build --

    def build_index(self, corpus: list[dict[str, Any]]) -> int:
        """corpus: [{doc_id, text, source_path, **meta}] -> full index rebuild."""
        import faiss

        texts = [c["text"] for c in corpus]
        vecs = self.embedder.encode(texts)
        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(vecs)
        import faiss as _faiss  # noqa: F401  (write uses the same handle)

        faiss.write_index(index, str(self.index_file))
        self._docs = [
            {k: v for k, v in c.items() if k != "text"} for c in corpus
        ]
        self.docs_file.write_text(
            json.dumps(self._docs, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self._index = index
        return len(corpus)

    # -- query --

    def search(self, query: str, k: int = 5) -> list[RetrievedDoc]:
        if self._index is None or not self._docs:
            return []
        qv = self.embedder.encode([query])
        scores, ids = self._index.search(qv, min(k, len(self._docs)))
        out = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0:
                continue
            doc = self._docs[idx]
            out.append(RetrievedDoc(
                doc_id=doc.get("doc_id", str(idx)),
                source_path=doc.get("source_path", ""),
                score=float(score),
                preview=doc.get("preview", "")[:200],
                meta={k: v for k, v in doc.items()
                      if k not in ("doc_id", "source_path", "preview")},
            ))
        return out


# ---- Corpus construction (submission-package JD.md -> corpus) ----

_JD_SECTION_RE = re.compile(r"## JD 原文\s*\n(.*)", re.S)
_RECORD_ID_RE = re.compile(r"\*\*记录 ID\*\*\s*\|\s*`([^`]+)`")
_TITLE_RE = re.compile(r"^\|\s*\*\*岗位\*\*\s*\|\s*([^|]+)\|", re.M)


def build_corpus_from_packages(packages_dir: str | Path) -> list[dict[str, Any]]:
    """Scan submission-package JD.md -> retrieval corpus (doc_id=record ID, text=JD original-text section)."""
    corpus = []
    for jd_path in sorted(Path(packages_dir).glob("*/JD.md")):
        text = jd_path.read_text(encoding="utf-8", errors="replace")
        m = _JD_SECTION_RE.search(text)
        if not m:
            continue
        rid = _RECORD_ID_RE.search(text)
        title = _TITLE_RE.search(text)
        body = m.group(1).strip()
        corpus.append({
            "doc_id": rid.group(1) if rid else jd_path.parent.name,
            "text": body,
            "source_path": str(jd_path),
            "title": (title.group(1).strip() if title else "")[:80],
            "preview": re.sub(r"\s+", " ", body)[:180],
        })
    return corpus
