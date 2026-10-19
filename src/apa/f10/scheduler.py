"""Windows Task Scheduler registration (design doc section 7: pollers use Task Scheduler,
not GitHub Actions cron — G9: Actions has a 5-minute minimum and can be delayed by a dozen minutes
at peak times, while pollers need to run on time).

Security discipline: dry-run by default only prints the schtasks commands that would be executed;
--apply actually registers them. Registration is a host-machine system change and must be
explicitly confirmed (isomorphic with the HITL discipline).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

TASK_PREFIX = "APAProbe"

# The three tasks: staged development plan, stage-4 spec (5min / 15min / daily)
TASK_SPECS = [
    {"name": f"{TASK_PREFIX}Statuspage", "minutes": 5,
     "args": "probe run --kind statuspage",
     "desc": "APA F10 status-page probing (status.careers.gov.sg, 5min)"},
    {"name": f"{TASK_PREFIX}Jobs", "minutes": 15,
     "args": "probe run --kind jobs",
     "desc": "APA F10 active job URL direct HEAD probing (15min)"},
    {"name": f"{TASK_PREFIX}Maintenance", "daily": "08:30",
     "args": "probe run --kind maintenance",
     "desc": "APA F10 maintenance-notice page probing (daily 08:30)"},
]


@dataclass
class TaskCommand:
    name: str
    command: str      # full schtasks command line (copy-paste executable)
    desc: str


def build_commands(apa_exe: str = "apa") -> list[TaskCommand]:
    """Build the three schtasks /Create commands (idempotent: /F overwrites same-named tasks)."""
    cmds = []
    for spec in TASK_SPECS:
        if "minutes" in spec:
            schedule = f"/sc minute /mo {spec['minutes']}"
        else:
            schedule = f"/sc daily /st {spec['daily']}"
        tr = f'"{apa_exe} {spec["args"]}"'
        cmds.append(TaskCommand(
            name=spec["name"],
            command=(f'schtasks /Create /TN "{spec["name"]}" {schedule} /TR {tr} /F '
                     f'/RL LIMITED'),
            desc=spec["desc"]))
    return cmds


def register(apply: bool = False, apa_exe: str = "apa") -> list[TaskCommand]:
    """dry-run returns the command list; apply=True executes schtasks item by item."""
    cmds = build_commands(apa_exe)
    if not apply:
        return cmds
    for c in cmds:
        subprocess.run(c.command, shell=True, check=True)
    return cmds


def unregister(apply: bool = False) -> list[str]:
    cmds = [f'schtasks /Delete /TN "{c.name}" /F' for c in build_commands()]
    if apply:
        for cmd in cmds:
            subprocess.run(cmd, shell=True, check=False)
    return cmds


def query_status() -> str:
    """Query the current status of the three tasks (schtasks /Query)."""
    out = []
    for spec in TASK_SPECS:
        r = subprocess.run(
            f'schtasks /Query /TN "{spec["name"]}" /FO LIST',
            shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.strip().startswith(("状态", "Status", "下次运行", "Next Run")):
                    out.append(f"{spec['name']}: {line.strip()}")
        else:
            out.append(f"{spec['name']}: not registered")
    return "\n".join(out)
