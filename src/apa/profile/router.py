"""R0 审阅路由：claims → 人该管哪几条（docs/20 §2.2 判据表）。

**这是 docs/20 的核心实现**：路由是纯函数、可单测；UI 只渲染它给出的队列。
这样 CLI 与 UI 行为一致（docs/11 薄指针纪律），且路由规则不必起 UI 就能验证。

设计依据（不是我的偏好，是业界反复验证的四条结论，信源见 docs/20 §1）：
1. **审全部 = 没审**（velam：400 条/天、中位 2 秒 → 产出橡皮图章不是判断）
   → 默认档是 ``auto``，不是「全部交给人」
2. **路由靠判据不靠感觉**（低置信 / 高影响 / 异常 / 抽样）
3. **policy override confidence**（高影响字段无论置信度都要人看）
4. **把决策框起来**（给具体问句，不是通用 approve）

本模块只做**分流与理由说明**，不做任何 claim 状态变更——变更只在
``apa.profile.merge`` 里发生（单一写路径）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .models import Claim, ClaimStatus


class Route(str, Enum):
    """分流档位。**AUTO 是默认档**——这是把 25 条降到 0-3 条的关键。"""

    AUTO = "auto"          # 直接用，不打扰
    CONFIRM = "confirm"    # 低成本确认（有出处可核对，非阻塞）
    DECIDE = "decide"      # 要人拍板（冲突 / 身份 / 高影响 / 异常）


#: 身份字段——**高影响**：填错身份 = 简历造假风险（A4 类事故）。
#: 注意 work_authorization 是枚举而非布尔。
IDENTITY_FIELDS = frozenset({
    "identity.work_authorization", "identity.full_name",
    "identity.email", "identity.phone",
})

#: 高影响经历字段：组织与职位写错 = 整份材料失真。
HIGH_STAKE_PREFIXES = ("experiences.0.org", "experiences.0.title")

#: 置信阈值：低于此值进 decide（对应 docs/20 §2.2「异常」判据）。
#: 结构化直取（JSON Resume）给 0.9，LLM 抽取从 0.7 起，多来源印证每次 +0.1。
CONFIDENCE_FLOOR = 0.6


@dataclass
class Routed:
    """一条 claim 的路由结果 + **人可读的判定理由**。

    ``reason`` 是给用户看的「为什么这条要你管」——docs/20 §1 结论：不给
    一堵输出墙，要说清为什么系统不确定。
    """

    claim: Claim
    route: Route
    reason: str

    @property
    def field(self) -> str:
        return self.claim.field

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.claim.field,
            "text": self.claim.text,
            "route": self.route.value,
            "reason": self.reason,
            "confidence": round(self.claim.confidence, 2),
            "status": self.claim.status.value,
            "sources": sorted({p.source_file for p in self.claim.provenance}),
            "locator": self.claim.provenance[0].locator if self.claim.provenance else "",
            "snippet": self.claim.provenance[0].snippet if self.claim.provenance else "",
            "options": self.options(),
            "rejected": [p.snippet for p in self.claim.rejected_sources
                         if p.extractor == "rejected-in-reingest"],
        }

    def options(self) -> list[str]:
        """冲突项给**两个具体选项**，不给「跳过」（docs/20 §2.3）。

        返回 [] 表示无需人选择（单来源确认 / 自动通过）。
        """
        if self.route is not Route.DECIDE:
            return []
        incoming = [p for p in self.claim.rejected_sources
                    if p.extractor == "rejected-in-reingest"]
        if incoming:
            return [self.claim.text, incoming[-1].snippet]
        if self.claim.status is ClaimStatus.NEEDS_HUMAN:
            return [self.claim.text, ""]        # 第二项 = 「补上 / 不适用」
        return []


def _sources(claim: Claim) -> set[str]:
    return {p.source_file for p in claim.provenance}


def route_claim(claim: Claim) -> Routed:
    """单条 claim 的路由判定（判据顺序即优先级）。"""
    # ── 1. 人的判断优先：已确认 / 已手改 → auto ──
    if claim.status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
        return Routed(claim, Route.AUTO, "you already settled this")

    # ── 2. 冲突 → decide（理由必须具体到「哪两个值」）──
    if claim.status is ClaimStatus.CONFLICT:
        incoming = [p for p in claim.rejected_sources
                    if p.extractor == "rejected-in-reingest"]
        if incoming:
            return Routed(claim, Route.DECIDE,
                          f"your documents disagree: \"{claim.text[:40]}\" vs "
                          f"\"{incoming[-1].snippet[:40]}\"")
        return Routed(claim, Route.DECIDE, "values conflict but the second side is gone")

    # ── 3. 人工标记要补充 → decide ──
    if claim.status is ClaimStatus.NEEDS_HUMAN:
        return Routed(claim, Route.DECIDE, "you flagged this as incomplete")

    # ── 4. 无出处 = 幻觉 → decide（无论置信度）──
    if not claim.provenance:
        return Routed(claim, Route.DECIDE,
                      "no source in your documents — I cannot show you where this came from")

    # ── 5. 身份字段 → decide（policy override confidence，docs/20 §1 结论 2）──
    if claim.field in IDENTITY_FIELDS:
        return Routed(claim, Route.DECIDE,
                      "identity field — a wrong value here is a false claim")

    # ── 6. 异常低置信 → decide ──
    if claim.confidence < CONFIDENCE_FLOOR:
        return Routed(claim, Route.DECIDE,
                      f"low confidence ({claim.confidence:.2f})")

    # ── 7. 多来源印证 → auto ──
    srcs = _sources(claim)
    if len(srcs) > 1:
        return Routed(claim, Route.AUTO, f"{len(srcs)} documents agree")

    # ── 8. 高影响经历字段 + 仅单来源 → decide（高影响期望代价）──
    if claim.field.startswith(HIGH_STAKE_PREFIXES):
        return Routed(claim, Route.DECIDE,
                      "organisation and title carry the most weight in a CV — "
                      "worth your eyes")

    # ── 9. 剩余单来源 → confirm（低成本，不阻塞）──
    return Routed(claim, Route.CONFIRM,
                  f"single source: {next(iter(srcs))} — skim if curious")


def route_all(claims: Iterable[Claim]) -> list[Routed]:
    """全部 claim → 路由结果，按 decide → confirm → auto 排序。"""
    order = {Route.DECIDE: 0, Route.CONFIRM: 1, Route.AUTO: 2}
    return sorted((route_claim(c) for c in claims),
                  key=lambda r: (order[r.route], r.field))


@dataclass
class ReviewQueue:
    """审阅队列视图（UI 与 CLI 共用同一结构）。"""

    decide: list[Routed] = field(default_factory=list)
    confirm: list[Routed] = field(default_factory=list)
    auto: list[Routed] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.decide) + len(self.confirm) + len(self.auto)

    @property
    def needs_human(self) -> int:
        return len(self.decide)

    def escalation_rate(self) -> float:
        """escalation rate（docs/20 §6 健康指标）。

        >50%  = 路由失效，瓶颈被重建（velam 的「you've rebuilt the bottleneck」）
        <1%   = 信号睡着了（不是「任务简单」——那是更常见的解释，要去查）
        """
        return (len(self.decide) / self.total) if self.total else 0.0


def build_queue(profile: Any) -> ReviewQueue:
    """Profile → 审阅队列。"""
    routed = route_all(profile.claims.values())
    q = ReviewQueue()
    for r in routed:
        if r.route is Route.DECIDE:
            q.decide.append(r)
        elif r.route is Route.CONFIRM:
            q.confirm.append(r)
        else:
            q.auto.append(r)
    return q


def health_metrics(queue: ReviewQueue) -> dict[str, Any]:
    """路由健康度（docs/20 §6）。**CLI 侧，不进 UI**（监控归 Rich 层）。"""
    rate = queue.escalation_rate()
    if queue.total == 0:
        verdict = "no claims yet"
    elif rate > 0.5:
        verdict = "routing is not doing its job — a bottleneck was rebuilt"
    elif rate < 0.01:
        verdict = "signals are asleep, not that the task is easy — check them"
    else:
        verdict = "healthy"
    return {
        "total_claims": queue.total,
        "decide": len(queue.decide),
        "confirm": len(queue.confirm),
        "auto": len(queue.auto),
        "escalation_rate": round(rate, 3),
        "verdict": verdict,
    }
