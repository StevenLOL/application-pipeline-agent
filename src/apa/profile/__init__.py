"""R0 个人档案：吸收用户文档 → 单一事实源（docs/19 §4）。


用户也能手动编辑更新，**不能让用户去手工填写**」。

模块分工（解析/抽取/合并严格分离，各自可单测）：

- ``models``   —— Claim（带 provenance 的事实句）/ Profile / WorkAuthorization
- ``loader``   —— docx/pdf/txt/md/json 解析（**零 LLM、零网络**）
- ``extract``  —— 候选发现（DSPy）+ **代码层逐字定位**（模型定位不到就丢弃）
- ``merge``    —— 三铁律：用户修正 > 新抽取 / 细化 > 替换 / 冲突不静默
- ``store``    —— profile.json 快照 + profile_history.jsonl 变更史（append-only）
"""

from .loader import Document, Segment, UnsupportedDocument, parse_dir, parse_document
from .merge import (MergeOutcome, apply_manual_edit, confirm_claim, mark_needs_human,
                    materialize_experiences, merge_claims, prune_drafts, resolve_conflict)
from .models import (Claim, ClaimStatus, DocFormat, Experience, Profile, Provenance,
                     WorkAuthorization)
from .store import history, load, record_change, review_queue, save, summarize

__all__ = [
    "Claim", "ClaimStatus", "DocFormat", "Document", "Experience", "MergeOutcome",
    "Profile", "Provenance", "Segment", "UnsupportedDocument", "WorkAuthorization",
    "apply_manual_edit", "confirm_claim", "history", "load", "mark_needs_human",
    "materialize_experiences", "merge_claims", "parse_dir", "parse_document",
    "prune_drafts", "record_change", "resolve_conflict", "review_queue", "save",
    "summarize",
]
