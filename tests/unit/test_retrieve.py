"""F5 retrieval stack v0 unit tests (FakeEmbedder offline — zero models, zero
network, deterministic)."""

from __future__ import annotations

import json

import pytest

from apa.retrieve import FakeEmbedder, Retriever, build_corpus_from_packages

CORPUS = [
    {"doc_id": "GOV-A", "text": "Senior AI Engineer role. RAG pipelines, LangGraph agentic workflows, vector search.",
     "source_path": "/pkg/A/JD.md", "title": "AI Engineer (Gov)", "preview": "Senior AI Engineer role."},
    {"doc_id": "GOV-B", "text": "Data policy and governance engineer. Privacy, audit, compliance frameworks.",
     "source_path": "/pkg/B/JD.md", "title": "Data Policy (Gov)", "preview": "Data policy and governance."},
    {"doc_id": "MCF-C", "text": "Product manager for consumer mobile apps. Roadmaps, A/B tests, growth metrics.",
     "source_path": "/pkg/C/JD.md", "title": "PM (Private)", "preview": "Product manager for consumer apps."},
]


def test_build_and_search_roundtrip(tmp_path):
    r = Retriever(FakeEmbedder(), tmp_path / "idx")
    n = r.build_index(CORPUS)
    assert n == 3
    assert (tmp_path / "idx" / "corpus.faiss").exists()
    assert len(json.loads((tmp_path / "idx" / "docs.json").read_text(encoding="utf-8"))) == 3

    # search: the RAG/LangGraph query should hit GOV-A (word-overlap driven
    # FakeEmbedder)
    hits = r.search("RAG LangGraph agentic workflows engineer", k=2)
    assert hits and hits[0].doc_id == "GOV-A"
    assert hits[0].score > 0
    assert hits[0].source_path == "/pkg/A/JD.md"


def test_search_persistence_across_instances(tmp_path):
    r1 = Retriever(FakeEmbedder(), tmp_path / "idx")
    r1.build_index(CORPUS)
    r2 = Retriever(FakeEmbedder(), tmp_path / "idx")  # a new instance recovers from disk
    hits = r2.search("privacy audit compliance", k=1)
    assert hits[0].doc_id == "GOV-B"


def test_search_empty_index(tmp_path):
    r = Retriever(FakeEmbedder(), tmp_path / "idx")
    assert r.search("anything") == []


def test_build_corpus_from_packages(tmp_path):
    pkg = tmp_path / "pkg" / "01_test_GOV-123"
    pkg.mkdir(parents=True)
    (pkg / "JD.md").write_text(
        "# T\n| **记录 ID** | `GOV-123` |\n| **岗位** | AI Engineer |\n"
        "## JD 原文\n\nJOBID: GOV-123\nClosing on 01 Nov 2026\n", encoding="utf-8")
    corpus = build_corpus_from_packages(tmp_path / "pkg")
    assert len(corpus) == 1
    assert corpus[0]["doc_id"] == "GOV-123"
    assert "Closing on 01 Nov 2026" in corpus[0]["text"]


def test_fake_embedder_deterministic():
    e = FakeEmbedder()
    v1 = e.encode(["hello world agentic"])
    v2 = e.encode(["hello world agentic"])
    assert v1.shape == (1, 256)
    assert (v1 == v2).all()
    assert abs(float((v1[0] ** 2).sum()) - 1.0) < 1e-5  # L2 normalization
