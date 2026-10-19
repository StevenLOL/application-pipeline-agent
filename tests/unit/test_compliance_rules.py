"""D2 规则单测：引擎分派 + template-residue（事故1）+ number-provenance（事故3）。

残留规则用**注入式负例**验证：把 ACME 信的独有句子注入 Helios 信副本，
规则必须命中——这复现事故1 的机制而非伪造"历史文件"（金标准版本对属 D3）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from apa.compliance.engine import (
    KIND_LAMBDA, KIND_MUST_HAVE, KIND_REGEX, Context, Rule, check_files, check_text,
)
from apa.compliance.rules.accidents import (
    Corpus, accident_rules, fork_of, number_provenance_check, template_residue_check,
)
from apa.compliance.rules.inherited_130 import inherited_rules

SINGAPORE = Path(r"<private-workspace>")
ME = SINGAPORE / "me"
_Helios = ME / "CoverLetter_Fabrikam_AI_Transformation_Helios_EN.md"
_ACME = ME / "CoverLetter_ACME_AI_Data_Office_EN.md"

needs_singapore = pytest.mark.skipif(not _Helios.is_file(), reason="singapore 工作区不在本机")

# 事故1 的残留原型句（ACME 信独有：部委数据域表述）
_ACME_RESIDUE = ("In a ministry the data already exists — workforce and employment data, "
                "workplace safety and health records, foreign-manpower administration")


# ── 引擎分派 ────────────────────────────────────────────────────────────

def test_engine_rule_kinds_and_severity():
    text = "alpha beta gamma"
    rules = [
        Rule(id="t-regex", code="T", desc="找 beta", kind=KIND_REGEX, hard=True, pattern=r"beta"),
        Rule(id="t-must", code="T", desc="必须含 delta", kind=KIND_MUST_HAVE, hard=True, must=r"delta"),
        Rule(id="t-lambda", code="T", desc="lambda 找 gamma", kind=KIND_LAMBDA, hard=False,
             fn=lambda t: [(1, "gamma")] if "gamma" in t else []),
    ]
    findings = check_text(text, rules)
    by_id = {f.rule_id: f for f in findings}
    assert by_id["t-regex"].hard and by_id["t-regex"].line == 1
    assert by_id["t-must"].hard and by_id["t-must"].line == 0
    assert not by_id["t-lambda"].hard


def test_engine_missing_file_and_counts():
    report = check_files([Path("/nonexistent/x.md")], inherited_rules()[:1])
    fr = report.files[0]
    assert fr.missing and "MISSING" in report.render_text()


def test_llm_rule_without_client_raises_not_silent():
    """llm 类规则已注册但未注入客户端 → 显式报错，不静默跳过（D3 前纪律）。"""
    from apa.compliance.engine import KIND_LLM

    rule = Rule(id="t-llm", code="T", desc="llm 规则", kind=KIND_LLM, hard=True,
                fn=lambda t, llm: [])
    with pytest.raises(RuntimeError, match="no LLM client was injected"):
        check_text("anything", [rule])


def test_engine_report_json_roundtrip():
    rep = check_files([_Helios], inherited_rules()) if _Helios.is_file() else None
    if rep is None:
        pytest.skip("singapore 不在本机")
    d = rep.to_dict()
    assert d["hard_count"] == sum(1 for f in d["files"] for x in f["findings"] if x["hard"])
    assert "findings" in d["files"][0]


# ── template-residue（事故1）────────────────────────────────────────────

def test_fork_of():
    assert fork_of("CoverLetter_ACME_AI_Data_Office_EN.md") == "ACME"
    assert fork_of("CoverLetter_Northwind_AI_Solution_Architect_EN.md") == "Northwind"
    assert fork_of("简历.md") is None


def test_residue_detects_injected_fork_sentence():
    """注入 ACME 独有句到 Helios 信 → 必须命中（事故1 机制复现）。

    语料用 me/ 全量 CL（真实使用形态）：多 fork 共有的头部块/共享业绩
    自动不判独有；断言为靶向命中（残留句在/不在），不对干净信断言零命中——
    共享业绩段可能合法触发告警，由人工复核（告警级定位）。
    """
    if not (_Helios.is_file() and _ACME.is_file()):
        pytest.skip("singapore 不在本机")
    helios = _Helios.read_text(encoding="utf-8")
    others = {
        f.name: f.read_text(encoding="utf-8")
        for f in sorted(ME.glob("CoverLetter_*.md"))
        if f.resolve() != _Helios.resolve()
    }
    corpus = Corpus(others=others, target_fork="Fabrikam")

    clean_segs = " ".join(seg for _, seg in template_residue_check(helios, corpus))
    assert "workforce and employment data" not in clean_segs, "干净信不应命中 ACME 残留句"

    injected = helios.replace(
        "I would welcome the opportunity to discuss",
        _ACME_RESIDUE + "\n\nI would welcome the opportunity to discuss",
    )
    hits = template_residue_check(injected, corpus)
    hit_segs = " ".join(seg for _, seg in hits)
    assert "workforce and employment data" in hit_segs, "注入的 ACME 残留句必须被检出"
    assert any("[ACME 独有内容]" in seg for _, seg in hits)
    assert all(line > 0 for line, _ in hits)


def test_residue_ignores_multifork_shared_achievement():
    """多 fork 共有的真实业绩（ten percentage points）不是残留——不误报。"""
    shared = "speech-recognition accuracy improved by ten percentage points across mass-produced vehicles"
    others = {
        "CoverLetter_ACME_AI_Data_Office_EN.md": f"At Zephyr {shared}.",
        "CoverLetter_Woodgrove_TRO_Transport_AI_Programme_EN.md": f"My work: {shared}.",
    }
    corpus = Corpus(others=others, target_fork="Fabrikam")
    hits = template_residue_check(f"I improved things: {shared}.", corpus)
    assert hits == [], "多 fork 共有内容不应被判为残留"


def test_residue_skips_non_cl_target():
    corpus = Corpus(others={"CoverLetter_ACME_AI_Data_Office_EN.md": "workforce data"}, target_fork=None)
    assert template_residue_check("workforce data", corpus) == []


# ── number-provenance（事故3）───────────────────────────────────────────

# D3 更新："165 tests" 已登记（2026-10-17 实跑 ragdemo：165 项 = 164 passed
# + 1 skipped，缺真实资产包 tome-1.7.6-gfx；docs/132:4）——从"待确认"转入
# 已登记列表。事故3 本体 "seven procurement packages" 保持未登记（金标准负例）。
@pytest.mark.parametrize("claim", ["seven procurement packages", "forty-two models"])
def test_unregistered_quantity_flagged(claim):
    hits = number_provenance_check(f"We delivered {claim} last year.")
    assert hits, f"未登记出处的 {claim} 必须被 flag"
    assert "needs manual reconciliation" in hits[0][1]


@pytest.mark.parametrize("claim", ["sixteen granted patents", "59/63", "31/31", "39/63",
                                   "ten percentage points", "eleven years in technology",
                                   "165 tests", "61 tests", "three granted vision patents"])
def test_registered_quantity_not_flagged(claim):
    assert number_provenance_check(f"Verified: {claim} in the record.") == []
