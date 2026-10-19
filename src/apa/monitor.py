"""F10 monitoring terminal dashboard (D11+ / layered architecture: monitoring view -> Rich Live).

Same pattern as the competitor (evidenced by ApplyPilot's apply/dashboard.py:
rich Table + status-color mapping + event log) - APA's monitoring view
(channel health / submission queue / latest probes) is isomorphic to its
scenario.

Usage: ``apa monitor --db data/apa.db --queue data/submission_queue.json``
(persistent Live refresh; Ctrl-C to quit. Read-only - monitoring writes no data.)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config
from .f10 import store

# State -> color (same mapping idea as ApplyPilot; APA's seven-state contract)
_STATE_STYLE = {
    "healthy": "bold green",
    "degraded": "yellow",
    "outage": "bold red",
    "recovering": "cyan",
    "unknown": "dim",
}
_RESULT_STYLE = {
    "ok": "green", "degraded": "yellow", "outage": "red",
    "blocked": "magenta", "gone": "dim", "parse_error": "yellow",
    "unreachable": "red",
}


def _health_table(db_path: Path, target_channel: dict[str, str]) -> Table:
    states = store.evaluate(db_path, target_channel) if db_path.exists() else {}
    t = Table(title="channel health (level set after 2 consecutive measurements; series granularity = target)", expand=True)
    t.add_column("channel", style="bold", min_width=16)
    t.add_column("state", width=12, justify="center")
    t.add_column("since", width=20)
    for ch in sorted(states):
        st = states[ch]
        style = _STATE_STYLE.get(st.state, "")
        since = st.since.strftime("%m-%d %H:%M") if st.since else "-"
        t.add_row(ch, Text(st.state.upper(), style=style), since)
    if not states:
        t.add_row("(no probe records)", "run `apa probe run` first", "-")
    return t


def _queue_table(queue_path: Path) -> Table:
    t = Table(title="submission queue (portal-health-gate view)", expand=True)
    t.add_column("JD", style="bold", min_width=16)
    t.add_column("channel", width=10)
    t.add_column("state", width=8)
    t.add_column("deadline", width=12)
    t.add_column("channel health", width=12, justify="center")
    try:
        data = json.loads(queue_path.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
    except (OSError, json.JSONDecodeError):
        entries = []
    if not entries:
        t.add_row("(queue empty or file not found)", "-", "-", "-", "-")
    for e in entries[:12]:
        health = str(e.get("channel_health") or "?")
        style = _STATE_STYLE.get(health, "")
        t.add_row(
            str(e.get("jd_id", "?")), str(e.get("channel", "?")),
            str(e.get("state", "?")), str(e.get("deadline", "-")),
            Text(health.upper(), style=style),
        )
    return t


def render(db_path: Path, queue_path: Path, target_channel: dict[str, str]) -> RenderableType:
    """Single-frame render (independently testable - does not depend on the Live loop)."""
    return Group(
        Panel(_health_table(db_path, target_channel), border_style="blue"),
        Panel(_queue_table(queue_path), border_style="cyan"),
    )


def run(db_path: Path, queue_path: Path, interval: float = 5.0) -> None:
    """Persistent Live loop (Ctrl-C to quit)."""
    from rich.live import Live

    tmap = {}
    if Path(config.PROBE_TARGETS_PATH).exists():
        try:
            targets = json.loads(Path(config.PROBE_TARGETS_PATH).read_text(encoding="utf-8"))["targets"]
            for t in targets:
                tmap.setdefault(t.get("channel", ""), t.get("channel", ""))
        except (json.JSONDecodeError, KeyError):
            pass
    with Live(render(db_path, queue_path, tmap), refresh_per_second=1 / interval) as live:
        while True:
            time.sleep(interval)
            live.update(render(db_path, queue_path, tmap))
