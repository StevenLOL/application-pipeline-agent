"""F10 channel health probing (planning doc section 3, F10 / staged development plan, stages 4-5).

Three poller paths (staged development plan, stage-4 spec):
  StatuspagePoller  status-page JSON (5min) — the careers.gov.sg Datadog/Statuspage instance
  JobUrlPoller      direct HEAD probe of active job URLs (15min) — ground truth for the channel's real service capability
  MaintenancePoller maintenance-notice page (daily)

Design discipline:
  - The transport layer is injectable (HttpTransport protocol): unit tests use FakeTransport to record
    responses, and CI forbids real network access; isomorphic with the engine's KIND_LLM — if not
    injected, raise explicitly instead of silently skipping.
  - The probe answers "is the channel in service", not "is the job open": an expired job's URL still
    returns 200 (measured 2026-10-18: an expired Initech role returns HEAD=200); job open/closed status
    is determined by the ledger/page content.
  - blocked (403/AccessDenied) = the probe environment is restricted, ≠ a channel outage; it must not
    count toward outages.
"""

from __future__ import annotations
