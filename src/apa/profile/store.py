"""R0 档案落盘：profile.json（当前真相）+ profile_history.jsonl（变更史）。

**append-only 纪律**（docs/08 §6 审计面）：任何 claim 状态变化都 append 一行
history，profile.json 只是「当前快照」。理由与 webapp 的 hitl_decisions.jsonl
同源——UI 崩溃/误关不丢已做的修正，且「我什么时候把 X 改成 Y」永远可查。

隐私：档案含姓名/邮箱/电话 → 落盘在 ``data/``（gitignored）+ **入库前过
PII 闸门**（``apa.privacy.assert_safe``），与全项目同一纪律。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .. import config
from .models import Profile

PROFILE_PATH = config.DATA_DIR / "profile.json"
HISTORY_PATH = config.DATA_DIR / "profile_history.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Optional[Path] = None) -> Profile:
    """读档案；不存在返回空 Profile（**不是错误**——首次 ingest 的正常起点）。"""
    p = Path(path or PROFILE_PATH)
    if not p.exists():
        return Profile()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"profile.json is unreadable ({exc}); move it aside to start over") from exc
    return Profile.from_dict(data)


def save(profile: Profile, path: Optional[Path] = None,
         materialize: bool = True) -> Path:
    """写快照。materialize=True 时先从 claim 重建 experiences 便捷视图。"""
    from .merge import materialize_experiences

    if materialize:
        materialize_experiences(profile)
    profile.sync_identity_from_claims()
    p = Path(path or PROFILE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def record_change(action: str, field_name: str, old: str, new: str,
                  reason: str = "", by: str = "human", path: Optional[Path] = None) -> dict:
    """append 一行变更史（append-only）。返回写入的记录。"""
    entry = {
        "ts": _now(),
        "action": action,
        "field": field_name,
        "old": old,
        "new": new,
        "reason": reason,
        "by": by,
    }
    log = Path(path or HISTORY_PATH)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def history(n: int = 20, path: Optional[Path] = None) -> list[dict]:
    """最近 n 条变更（CLI/UI 展示）。"""
    log = Path(path or HISTORY_PATH)
    if not log.exists():
        return []
    lines = [l for l in log.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
    out: list[dict] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def review_queue(profile: Profile, limit: int = 50) -> list[dict[str, Any]]:
    """审阅队列：**needs_human / conflict 优先置顶**，然后未确认的 draft。

    排序即优先级（docs/19 §4.3）：用户最该看的不是「最不确定的 50 条」，
    而是「挡在材料生成前面的那几条」。
    """
    rank = {"conflict": 0, "needs_human": 1, "draft": 2, "confirmed": 3,
            "manual_override": 4}
    rows = [{"field": k, "text": c.text, "status": c.status.value,
             "confidence": round(c.confidence, 2),
             "sources": sorted({p.source_file for p in c.provenance}),
             "locator": c.provenance[0].locator if c.provenance else "",
             "snippet": c.provenance[0].snippet if c.provenance else "",
             "has_conflict": bool(c.rejected_sources)}
            for k, c in profile.claims.items()]
    rows.sort(key=lambda r: (rank.get(r["status"], 5), r["field"]))
    return rows[:limit]


def summarize(profile: Profile) -> dict[str, Any]:
    """档案概览（CLI/UI 首屏数字）——不含 PII 明文，只有计数与状态分布。"""
    by_status: dict[str, int] = {}
    for c in profile.claims.values():
        by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
    srcs = sorted({p.source_file for c in profile.claims.values()
                   for p in c.provenance})
    return {
        "total_claims": len(profile.claims),
        "by_status": by_status,
        "experiences": len(profile.experiences),
        "pinned": len(profile.pinned_fields()),
        "source_documents": srcs,
        "has_email": bool(profile.email),
        "has_work_authorization": profile.work_authorization.value != "not_stated",
    }
