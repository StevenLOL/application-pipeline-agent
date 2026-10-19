"""R0 profile persistence: profile.json (current truth) + profile_history.jsonl (change history).

**Append-only discipline** (docs/08 §6 audit surface): every claim status change
appends one history line; profile.json is just the "current snapshot". The rationale
is the same as the webapp's hitl_decisions.jsonl — a UI crash/accidental close must
not lose corrections already made, and "when did I change X to Y" stays queryable
forever.

Privacy: the profile contains name/email/phone -> it is written under ``data/``
(gitignored) + **passes the PII gate before any egress** (``apa.privacy.assert_safe``),
the same discipline as the rest of the project.
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
    """Load the profile; return an empty Profile when absent (**not an error** — the normal starting point of a first ingest)."""
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
    """Write the snapshot. When materialize=True, first rebuild the experiences convenience view from claims."""
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
    """Append one change-history line (append-only). Returns the written record."""
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
    """The most recent n changes (CLI/UI display)."""
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
    """Review queue: **needs_human / conflict items are pinned to the top first**,
    then unconfirmed drafts.

    The sort order is the priority (docs/19 §4.3): what the user should look at
    first is not "the 50 most uncertain items" but "the few items blocking
    material generation".
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
    """Profile overview (the CLI/UI first-screen numbers) — no PII plaintext, only counts and status distribution."""
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
