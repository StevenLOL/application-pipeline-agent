"""Three poller paths: probe execution + result classification.

Classification contract (the outcome enum, planning doc section 5, probe_log.outcome):
  ok          the channel is serving normally
  degraded    partially impaired (status-page minor / job 5xx / inside a maintenance window)
  outage      channel failure (status-page major|critical / transport layer unreachable)
  blocked     probe environment restricted (403/AccessDenied/S3 rejection) — not a channel failure
  gone        job URL 404 (the job has been taken down; an F12 signal, not a channel failure)
  parse_error the response arrived but its content could not be parsed
  unreachable transport-layer failure that cannot be classified (DNS/timeout/connection refused)

Statuspage notes (verified 2026-10-18): the careers.gov.sg status-page instance
(status.careers.gov.sg, the new Atlassian Statuspage SPA) is distributed via CloudFront+S3,
and /api/v2/*.json returns S3 AccessDenied to non-browser clients (verified with multiple
UA/header combinations). So StatuspagePoller is implemented against the standard Statuspage API
contract; the first run will faithfully record blocked; this is a record of a probe-environment
limitation, not "the status page is broken".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .http import HttpTransport, HttpResponse

# outcome enum (consistent with the planning doc section 5 probe_log.outcome)
OUT_OK = "ok"
OUT_DEGRADED = "degraded"
OUT_OUTAGE = "outage"
OUT_BLOCKED = "blocked"
OUT_GONE = "gone"
OUT_PARSE_ERROR = "parse_error"
OUT_UNREACHABLE = "unreachable"

KIND_STATUSPAGE = "statuspage"
KIND_JOB_URL = "job_url"
KIND_MAINTENANCE = "maintenance"


@dataclass
class ProbeResult:
    kind: str
    target: str
    outcome: str
    detail: str = ""
    latency_ms: int = 0
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_row(self) -> tuple:
        return (self.ts, self.kind, self.target, self.outcome, self.detail, self.latency_ms)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_s3_access_denied(resp: HttpResponse) -> bool:
    """CloudFront+S3 origin rejection signature: 403/401 with an S3 AccessDenied XML body."""
    if resp.status in (401, 403):
        return "AccessDenied" in (resp.body or "")[:2048] or not resp.body
    return False


class StatuspagePoller:
    """Status-page JSON probing (5-minute tier).

    Standard Statuspage API: summary.json preferred (contains page+components+incidents),
    falling back to status.json + components.json on failure. indicator mapping:
      none→ok / minor→degraded / major|critical→outage / maintenance→degraded
    """

    def __init__(self, base_url: str, transport: HttpTransport,
                 endpoints: tuple[str, ...] = ("/api/v2/summary.json",)):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.endpoints = endpoints

    def probe(self) -> list[ProbeResult]:
        results = []
        for ep in self.endpoints:
            url = self.base_url + ep
            t0 = time.monotonic()
            resp = self.transport.request(url)
            latency = int((time.monotonic() - t0) * 1000)
            results.append(self._classify(url, resp, latency))
            # once the summary endpoint parses, do not degrade-request the remaining endpoints
            if results[-1].outcome in (OUT_OK, OUT_DEGRADED, OUT_OUTAGE):
                break
        return results

    def _classify(self, url: str, resp: HttpResponse, latency: int) -> ProbeResult:
        if resp.error is not None:
            return ProbeResult(KIND_STATUSPAGE, url, OUT_UNREACHABLE, resp.error, latency)
        if _is_s3_access_denied(resp):
            return ProbeResult(KIND_STATUSPAGE, url, OUT_BLOCKED,
                               f"HTTP {resp.status}: the origin rejects non-browser clients "
                               "(S3 AccessDenied, a probe-environment limitation, not a channel failure)", latency)
        if resp.status != 200:
            return ProbeResult(KIND_STATUSPAGE, url, OUT_DEGRADED,
                               f"HTTP {resp.status}", latency)
        try:
            data = json.loads(resp.body)
        except json.JSONDecodeError as e:
            return ProbeResult(KIND_STATUSPAGE, url, OUT_PARSE_ERROR,
                               f"non-JSON response (first 80 chars: {resp.body[:80]!r}): {e}", latency)
        page = data.get("page") or {}
        indicator = str(page.get("status_indicator", "none")).lower()
        description = str(page.get("status_description", ""))
        mapping = {"none": OUT_OK, "minor": OUT_DEGRADED, "major": OUT_OUTAGE,
                   "critical": OUT_OUTAGE, "maintenance": OUT_DEGRADED}
        outcome = mapping.get(indicator, OUT_PARSE_ERROR)
        n_comp = len(data.get("components") or [])
        n_inc = len(data.get("incidents") or [])
        return ProbeResult(KIND_STATUSPAGE, url, outcome,
                           f"indicator={indicator} ({description}); "
                           f"components={n_comp} incidents={n_inc}", latency)


@dataclass
class JobPageSignals:
    """Deep-inspection signals from a GET of the job detail page (observational; not inputs to outcome judgment).

    Verified 2026-10-19 (cross-validated after a hint that closed positions display a "closed" label):
    the deadline/closed labels in the careers.gov.sg detail page's SSR are **unstable** —
    two GETs of the same URL: `Closing on DD Mon YYYY` appears or not at random; the `>Closed<` span
    appears dynamically and sits next to an SVG icon (suspected to be a UI component rather than a job
    status); `datePosted` differs between the two (10-19 vs 09-19). The observed `Closing in N day(s)`
    is not in the detail page's SSR (it presumably belongs to the list page / JS-rendered state).
    → Conclusion: record these signals in probe_log for manual assessment, **not as automatic-judgment inputs**.
    """

    closing_on: str | None = None      # extracted value of "Closing on 27 Oct 2026"
    closed_span: bool = False          # the page contains a >Closed< span (authenticity pending manual review)
    date_posted: str | None = None     # JSON-LD datePosted
    body_len: int = 0


_CLOSING_ON_RE = re.compile(r"Closing on\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.I)
_CLOSED_SPAN_RE = re.compile(r">\s*Closed\s*<", re.I)
_DATEPOSTED_RE = re.compile(r'"datePosted"\s*:\s*"([^"]+)"')


def parse_job_page(body: str) -> JobPageSignals:
    """Extract deadline/closed/posted-date signals from a job detail page's HTML (pure function)."""
    return JobPageSignals(
        closing_on=(_CLOSING_ON_RE.search(body).group(1)
                    if _CLOSING_ON_RE.search(body) else None),
        closed_span=bool(_CLOSED_SPAN_RE.search(body)),
        date_posted=(_DATEPOSTED_RE.search(body).group(1)
                     if _DATEPOSTED_RE.search(body) else None),
        body_len=len(body),
    )


class JobUrlPoller:
    """Direct HEAD probe of active job URLs (15-minute tier) — ground truth for the channel's service capability.

    Classification: 2xx/3xx→ok; 403/429→blocked (**invisible to this probe**, ≠ a channel failure);
    404→gone (the job has been taken down); 5xx→degraded; transport failure→unreachable.
    When HEAD is rejected with 405, the caller retries with GET (handled inside this class: 405
    automatically degrades to GET once).

    ⚠️ **The correct reading of blocked (corrected 2026-10-19)**: 403/empty body is **not channel anti-scraping**.
    Measured evidence from the browser-render experiments:
      ① the SPA was not waited for long enough → after ``waitForTimeout(6000~10000)`` the body has content
         (original text: "Workday-type SPAs must be waited for, otherwise the body is empty");
      ② some sites require a logged-in state → log in via a browser first and reuse the profile
         (experiment conclusion: Workday requires Singpass).
    So blocked = **the capability ceiling of this HTTP probe**; the fix is to switch to the rendering
    channel of ``apa.f10.browser`` (``--browser``), not to record the channel as failed.

    With `deep=True`, GET the page body and extract JobPageSignals (deadline/closed/posted date),
    recorded in detail for manual assessment — **not an input to outcome judgment** (SSR labels are
    unstable; see the JobPageSignals docstring evidence). Deep mode is for on-demand inspection only,
    off by default.
    """

    def __init__(self, transport: HttpTransport):
        self.transport = transport

    def probe_one(self, target: str, deep: bool = False) -> ProbeResult:
        t0 = time.monotonic()
        resp = self.transport.request(target, method="HEAD")
        latency = int((time.monotonic() - t0) * 1000)
        if resp.status == 405:  # some ATSs do not accept HEAD → degrade to GET
            t0 = time.monotonic()
            resp = self.transport.request(target, method="GET")
            latency = int((time.monotonic() - t0) * 1000)
        if not deep:
            return self._classify(target, resp, latency)
        # deep: the HEAD conclusion is produced as usual; re-fetching the body is only worthwhile when HEAD is 2xx/3xx
        result = self._classify(target, resp, latency)
        if result.outcome != OUT_OK or resp.error is not None:
            return result
        t0 = time.monotonic()
        body_resp = self.transport.request(target, method="GET")
        extra_ms = int((time.monotonic() - t0) * 1000)
        if body_resp.error is not None or body_resp.status != 200:
            return result
        sig = parse_job_page(body_resp.body or "")
        result.detail += (
            f" | deep: closing_on={sig.closing_on or '-'}"
            f" closed_span={'Y' if sig.closed_span else 'N'}"
            f" posted={sig.date_posted or '-'}"
            f" len={sig.body_len}"
        )
        result.latency_ms += extra_ms
        return result

    def probe(self, targets: list[str], deep: bool = False) -> list[ProbeResult]:
        return [self.probe_one(t, deep=deep) for t in targets]

    def _classify(self, target: str, resp: HttpResponse, latency: int) -> ProbeResult:
        if resp.error is not None:
            return ProbeResult(KIND_JOB_URL, target, OUT_UNREACHABLE, resp.error, latency)
        s = resp.status or 0
        if 200 <= s < 400:
            return ProbeResult(KIND_JOB_URL, target, OUT_OK, f"HTTP {s}", latency)
        if s in (401, 403, 429):
            return ProbeResult(KIND_JOB_URL, target, OUT_BLOCKED,
                               f"HTTP {s}: anti-scraping/rate-limited (the channel may be serving; the probe was rejected)", latency)
        if s == 404:
            return ProbeResult(KIND_JOB_URL, target, OUT_GONE,
                               "HTTP 404: the job page has been taken down (an F12 signal, not a channel failure)", latency)
        if 500 <= s < 600:
            return ProbeResult(KIND_JOB_URL, target, OUT_DEGRADED, f"HTTP {s}", latency)
        return ProbeResult(KIND_JOB_URL, target, OUT_PARSE_ERROR, f"HTTP {s}", latency)


class MaintenancePoller:
    """Maintenance-notice page probing (daily tier).

    When the target page is an SPA, the notice content cannot be parsed → record parse_error/blocked with
    an explanation; when it can be parsed, extract the count of in-progress/announced maintenance windows
    (only counts if the page has parseable markers).
    """

    # characteristics of the page containing parseable maintenance data (old/new Atlassian Statuspage page markers)
    _MAINTENANCE_MARKERS = re.compile(
        r"scheduled[- ]maintenance|maintenance[- ]window|Planned Maintenance", re.I)

    def __init__(self, url: str, transport: HttpTransport):
        self.url = url
        self.transport = transport

    def probe(self) -> ProbeResult:
        t0 = time.monotonic()
        resp = self.transport.request(self.url)
        latency = int((time.monotonic() - t0) * 1000)
        if resp.error is not None:
            return ProbeResult(KIND_MAINTENANCE, self.url, OUT_UNREACHABLE, resp.error, latency)
        if _is_s3_access_denied(resp):
            return ProbeResult(KIND_MAINTENANCE, self.url, OUT_BLOCKED,
                               f"HTTP {resp.status}: the origin rejects non-browser clients", latency)
        if resp.status != 200:
            return ProbeResult(KIND_MAINTENANCE, self.url, OUT_DEGRADED,
                               f"HTTP {resp.status}", latency)
        body = resp.body or ""
        if len(body) < 4096 and "<script" in body and "status-pages-site" in body:
            # SPA shell: no server-rendered content to parse
            return ProbeResult(KIND_MAINTENANCE, self.url, OUT_PARSE_ERROR,
                               "SPA shell page (no SSR content); maintenance notices need browser rendering or "
                               "await another exposure method; manual announcement archiving remains the main channel", latency)
        n = len(self._MAINTENANCE_MARKERS.findall(body))
        return ProbeResult(KIND_MAINTENANCE, self.url, OUT_OK,
                           f"the page is parseable, {n} maintenance-related marker(s)", latency)
