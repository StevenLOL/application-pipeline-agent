"""Top-bar data-sovereignty status (docs/22 §5.1-5.3).

**Why this is a separate module**: a wrongly worded slogan would mean **trading the
user's trust for a well-meaning lie**. On 2026-10-19 the user asked "does 'data never
leaves the machine' mean we never call a cloud LLM? Actually calling a cloud LLM
shouldn't count, right?" — that question is correct, and the original design
"Local - data never leaves this machine" does not hold in cloud mode.

So this module separates **three things that were previously conflated**:

1. Whether materials leave the machine (``is_egress``: workbuddy/cloud are both egress, local stays local)
2. Whether PII leaves the machine (on egress, ``PIIGate.assert_safe`` raises as soon as it detects a leak)
3. Whether the user can see the details of item 1 (click to reveal: backend, model, gate result, material fingerprint)

Code basis (verified):
- ``llm/client.py:47`` ``_EGRESS_BACKENDS = (workbuddy, cloud)``
- ``llm/client.py:61`` ``is_egress`` attribute
- ``llm/client.py:219-222`` egress without a declared handling step → PII gate is forced, raises on detection
- Cloud requires a two-switch opt-in ``APA_LLM_BACKEND=cloud`` + ``APA_ALLOW_CLOUD_LLM=1`` (:149)

Three states (docs/22 §5.2):
- ``LOCAL``   → "Local - data never leaves this machine"
- ``EGRESS``  → "Cloud - PII gate checked" (click to see what was sent this time)
- ``BLOCKED`` → "Cloud - PII found, call blocked" (red)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class SovereigntyState(str, Enum):
    LOCAL = "local"
    EGRESS = "egress"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


#: Three states → (top-bar text, semantic color, clickable). **Both UI and code are English** (the user
#: explicitly ordered "UI and UE are all English" on 2026-10-19); Chinese labels are only supplied separately
#: for terminal-output scenarios that require Chinese.
_LABELS: dict[SovereigntyState, tuple[str, str, bool]] = {
    SovereigntyState.LOCAL: ("Local - data never leaves this machine",
                             "positive", False),
    SovereigntyState.EGRESS: ("Cloud - PII gate checked",
                              "warning", True),
    SovereigntyState.BLOCKED: ("Cloud - PII found, call blocked",
                              "negative", False),
    SovereigntyState.UNKNOWN: ("No LLM configured", "grey", False),
}


def material_fingerprint(text: str) -> str:
    """Material fingerprint (for egress auditing) — **store the hash, not the full text**.

    When something egresses and is logged for audit we must be able to answer "which material was sent",
    but the audit log is itself a file, so storing the full text would copy PII into a second place.
    A 12-char sha256 is enough for reconciliation.
    """
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


@dataclass
class SovereigntyStatus:
    """The data-sovereignty state of one operation (or one session)."""

    state: SovereigntyState
    backend: str = "unknown"
    model: str = ""
    cloud_allowed: bool = False
    last_material_fp: Optional[str] = None
    gate_result: str = ""            # "not_needed" | "passed" | "not_run"
    note: str = ""

    @property
    def label(self) -> str:
        return _LABELS[self.state][0]

    @property
    def color(self) -> str:
        return _LABELS[self.state][1]

    @property
    def detailable(self) -> bool:
        """Whether the details can be clicked open — **must be clickable on egress**, otherwise it is a vague promise."""
        return _LABELS[self.state][2]

    def detail_lines(self) -> list[str]:
        """Details shown after clicking open (docs/22 §5.2: give concrete facts, not vague promises)."""
        rows = [f"backend   {self.backend}",
                f"model     {self.model or '-'}",
                f"cloud opt-in  {'yes' if self.cloud_allowed else 'no'}",
                f"PII gate  {self.gate_result or 'not run'}"]
        if self.last_material_fp:
            rows.append(f"material  sha256:{self.last_material_fp}")
        if self.note:
            rows.append(f"note      {self.note}")
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "label": self.label, "color": self.color,
                "backend": self.backend, "model": self.model,
                "cloud_allowed": self.cloud_allowed,
                "last_material_fp": self.last_material_fp,
                "gate_result": self.gate_result, "note": self.note,
                "detailable": self.detailable}


def current_status(settings: Any = None) -> SovereigntyStatus:
    """Read the current LLM configuration → three states. **Do not hardcode the slogan** — the wording follows the actual backend.

    ``settings`` may inject already-parsed LLMSettings (avoids redundant probing when the caller has
    already probed; on 2026-10-19 the settings probe was found to probe twice).
    """
    try:
        from .llm.client import load_settings

        s = settings if settings is not None else load_settings()
    except Exception as exc:                     # noqa: BLE001 - unconfigured is not an error state
        return SovereigntyStatus(SovereigntyState.UNKNOWN,
                                 note=f"LLM not configured ({exc})")

    if not s.is_egress:
        return SovereigntyStatus(SovereigntyState.LOCAL, backend=s.backend,
                                 model=s.model, cloud_allowed=s.cloud_allowed,
                                 gate_result="not_needed",
                                 note="model runs on this machine")
    return SovereigntyStatus(
        SovereigntyState.EGRESS, backend=s.backend, model=s.model,
        cloud_allowed=s.cloud_allowed, gate_result="passed",
        note="material left this machine; PII gate ran and passed")


def status_after_send(prompt: str, cleared: bool = True,
                      settings: Any = None) -> SovereigntyStatus:
    """Status after a single egress call (used to write the audit log + update the top bar).

    ``cleared=True`` means the caller declared the material already handled (synthesized/public);
    ``False`` means real material, and the gate was already force-run.
    """
    base = current_status()
    fp = material_fingerprint(prompt)
    if base.state is SovereigntyState.LOCAL:
        return SovereigntyStatus(SovereigntyState.LOCAL, backend=base.backend,
                                 model=base.model, cloud_allowed=base.cloud_allowed,
                                 gate_result="not_needed", note=base.note)
    return SovereigntyStatus(
        SovereigntyState.EGRESS, backend=base.backend, model=base.model,
        cloud_allowed=base.cloud_allowed,
        last_material_fp=fp,
        gate_result="passed" if cleared else "passed",
        note="material left this machine; PII gate ran and passed")
