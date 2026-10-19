"""R0 审阅路由测试（docs/20）。

钉死的核心纪律：**默认档是 auto，不是「全部交给人」**。
证据来源：实测 `data/profile.json` 的 25 条 draft / 单一来源，按平铺设计
用户要面对 25 张卡；按本模块的路由，该进 decide 队列的是少数。

判据优先级（docs/20 §2.2）：人的判断 > 冲突 > 人工标记 > 无出处 >
身份字段 > 低置信 > 多来源印证 > 高影响单来源 > 其余单来源。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apa.profile.models import Claim, ClaimStatus, Profile, Provenance
from apa.profile.router import (CONFIDENCE_FLOOR, ReviewQueue, Route, build_queue,
                                health_metrics, route_all, route_claim)

NOW = "2026-10-19T00:00:00+00:00"


def claim(field: str = "skills.0", text: str = "Python", status: ClaimStatus = ClaimStatus.DRAFT,
          confidence: float = 0.7, sources: tuple[str, ...] = ("cv.docx",),
          rejected: list[Provenance] | None = None) -> Claim:
    """构造一条 claim。

    注意：带 ``rejected`` 的**冲突**必须同时是 ``CONFLICT`` 状态——真实路径上
    两者由 ``apa.profile.merge.merge_claims`` 一起产生（不是测试特例），
    这里显式要求以免写出「有分歧记录但状态是 draft」的非法组合。
    """
    rejected = rejected or []
    if rejected and status is ClaimStatus.DRAFT:
        status = ClaimStatus.CONFLICT
    return Claim(
        field=field, text=text, status=status, confidence=confidence,
        provenance=[Provenance(src, f"para {i}", text[:80], NOW)
                    for i, src in enumerate(sources)],
        rejected_sources=rejected)


def incoming_snippet(text: str) -> Provenance:
    """构造一条「re-ingest 带进来的新值」标记（merge 的冲突路径产物）。"""
    return Provenance("cv2.pdf", "page 2", text, NOW,
                      extractor="rejected-in-reingest")


# ================= 单条路由 =================

class TestRouteClaim:
    def test_settled_claims_are_auto(self):
        for status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
            assert route_claim(claim(status=status)).route is Route.AUTO

    def test_conflict_goes_to_decide_with_both_values_named(self):
        """docs/20 §1 结论 4：理由要具体到两个值，不是「有冲突」。"""
        c = claim(field="identity.location", text="Singapore",
                  rejected=[incoming_snippet("Tianjin")])
        r = route_claim(c)
        assert r.route is Route.DECIDE
        assert "Singapore" in r.reason and "Tianjin" in r.reason
        assert r.options() == ["Singapore", "Tianjin"]

    def test_needs_human_goes_to_decide(self):
        r = route_claim(claim(status=ClaimStatus.NEEDS_HUMAN))
        assert r.route is Route.DECIDE and "flagged" in r.reason

    def test_no_provenance_is_always_decide_even_if_confident(self):
        """无出处 = 幻觉嫌疑。置信度再高也要人看。"""
        c = claim(confidence=0.99)
        c.provenance = []
        r = route_claim(c)
        assert r.route is Route.DECIDE and "no source" in r.reason

    @pytest.mark.parametrize("field", sorted(
        {"identity.full_name", "identity.email", "identity.phone",
         "identity.work_authorization"}))
    def test_identity_fields_override_confidence(self, field):
        """policy override confidence：身份字段无论多自信都要人拍板。"""
        r = route_claim(claim(field=field, confidence=0.99))
        assert r.route is Route.DECIDE and "identity" in r.reason

    def test_low_confidence_goes_to_decide(self):
        r = route_claim(claim(confidence=CONFIDENCE_FLOOR - 0.01))
        assert r.route is Route.DECIDE and "low confidence" in r.reason

    def test_confidence_exactly_at_floor_is_not_decide(self):
        r = route_claim(claim(confidence=CONFIDENCE_FLOOR))
        assert r.route is Route.CONFIRM

    def test_multi_source_agreement_is_auto(self):
        r = route_claim(claim(sources=("cv.docx", "resume.pdf")))
        assert r.route is Route.AUTO and "2 documents agree" in r.reason

    def test_high_stake_single_source_goes_to_decide(self):
        r = route_claim(claim(field="experiences.0.org", text="AdventureWorks"))
        assert r.route is Route.DECIDE and "organisation" in r.reason

    def test_ordinary_single_source_is_confirm_not_decide(self):
        """这是「繁琐」的正解：普通字段不该阻塞用户。"""
        r = route_claim(claim(field="experiences.1.bullets.0"))
        assert r.route is Route.CONFIRM and "single source" in r.reason

    def test_decide_never_offers_a_skip_option(self):
        """docs/20 §2.3：decide 只有两个具体选项，没有「跳过」。"""
        r = route_claim(claim(field="identity.full_name"))
        assert len(r.options()) <= 2
        assert "" not in r.options() or r.claim.status is ClaimStatus.NEEDS_HUMAN

    def test_non_decide_has_no_options(self):
        assert route_claim(claim()).options() == []


# ================= 队列 =================

class TestBuildQueue:
    def test_draft_soup_routes_mostly_to_confirm_not_decide(self):
        """实测形态：25 条 draft 来自单一来源 → decide 应为少数。

        这是对 docs/19 §4.3「平铺全部 claim」的直接否定。
        """
        p = Profile()
        for i in range(25):
            p.claims[f"experiences.0.bullets.{i}"] = claim(
                field=f"experiences.0.bullets.{i}")
        q = build_queue(p)
        assert q.needs_human == 0                 # 无身份字段、无冲突 → 不阻塞
        assert len(q.confirm) == 25
        assert len(q.auto) == 0
        assert q.escalation_rate() == 0.0

    def test_queue_partitions_everything(self):
        p = Profile()
        p.claims["k_a"] = claim(field="skills.0")                       # confirm
        p.claims["k_b"] = claim(field="skills.1", sources=("x", "y"))   # auto
        p.claims["k_c"] = claim(field="identity.email")                  # decide
        p.claims["k_d"] = claim(field="skills.2",
                                status=ClaimStatus.CONFIRMED)           # auto
        q = build_queue(p)
        assert q.total == 4
        assert len(q.decide) + len(q.confirm) + len(q.auto) == 4
        assert {r.field for r in q.decide} == {"identity.email"}
        assert {r.field for r in q.auto} == {"skills.1", "skills.2"}
        assert {r.field for r in q.confirm} == {"skills.0"}

    def test_empty_profile_has_zero_rate_not_divide_by_zero(self):
        q = build_queue(Profile())
        assert q.total == 0 and q.escalation_rate() == 0.0

    def test_route_all_sorts_decide_first(self):
        p = Profile()
        p.claims["z_confirm"] = claim(field="skills.9")
        p.claims["a_decide"] = claim(field="identity.full_name")
        order = [r.route for r in route_all(p.claims.values())]
        assert order[0] is Route.DECIDE


# ================= 健康指标 =================

class TestHealthMetrics:
    def test_healthy_band(self):
        p = Profile()
        for i in range(10):
            p.claims[f"k{i}"] = claim(field=f"skills.{i}")
        p.claims["k_id"] = claim(field="identity.email")
        p.claims["k_id2"] = claim(field="identity.phone")
        m = health_metrics(build_queue(p))
        assert m["total_claims"] == 12 and m["decide"] == 2
        assert m["escalation_rate"] == round(2 / 12, 3) and m["verdict"] == "healthy"

    def test_bottleneck_rebuilt_is_called_out(self):
        """velam: 60% escalate = 'you have rebuilt the bottleneck'。"""
        p = Profile()
        for f in ("identity.email", "identity.phone", "identity.full_name",
                  "identity.work_authorization"):
            p.claims[f] = claim(field=f)
        m = health_metrics(build_queue(p))
        assert m["escalation_rate"] == 1.0
        assert "bottleneck" in m["verdict"]

    def test_signals_asleep_is_called_out(self):
        """<1% escalate 不等于「任务简单」——那通常是信号睡着了。"""
        p = Profile()
        for i in range(300):
            p.claims[f"skills.{i}"] = claim(field=f"skills.{i}",
                                             sources=("a", "b"))
        m = health_metrics(build_queue(p))
        assert m["escalation_rate"] == 0.0 and "asleep" in m["verdict"]

    def test_no_claims_says_so(self):
        assert health_metrics(build_queue(Profile()))["verdict"] == "no claims yet"


# ================= 与 store / UI 的接口一致性 =================

class TestQueueSerialisation:
    def test_routed_to_dict_carries_reason_and_options(self):
        c = claim(field="identity.location", text="Singapore",
                  rejected=[incoming_snippet("Tianjin")])
        d = route_claim(c).to_dict()
        assert d["route"] == "decide"
        assert d["options"] == ["Singapore", "Tianjin"]
        assert d["rejected"] == ["Tianjin"]
        assert json.loads(json.dumps(d))["reason"]           # 可序列化

    def test_reason_always_non_empty(self):
        """docs/20 §1：每条都要说清「为什么这条要你管」，不能白进队列。"""
        variants = [
            claim(),
            claim(field="identity.full_name"),
            claim(sources=("a", "b")),
            claim(confidence=0.1),
            claim(status=ClaimStatus.CONFIRMED),
            claim(field="experiences.0.org"),
        ]
        for c in variants:
            assert route_claim(c).reason.strip()

    def test_real_profile_file_routes_sanely(self):
        """若 data/profile.json 存在，用它做一次真实形状的回归。"""
        from apa.profile.models import Profile as P

        path = Path(__file__).resolve().parents[2] / "data" / "profile.json"
        if not path.exists():
            pytest.skip("no live profile.json")
        p = P.from_dict(json.loads(path.read_text(encoding="utf-8")))
        q = build_queue(p)
        # 真实档案必须给出非平凡的队列，且不该把全部 claim 都塞进 decide
        assert q.total == len(p.claims)
        assert q.needs_human < q.total or q.needs_human == 0
        assert all(r.reason.strip() for r in q.decide + q.confirm + q.auto)
