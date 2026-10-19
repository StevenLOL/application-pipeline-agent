"""HTTP transport abstraction: the single point of contact between probes and the network.

The default is UrllibTransport (stdlib urllib, browser UA + timeout); tests inject
FakeTransport to replay recorded responses. urllib was chosen over requests: the apa environment has zero third-party
HTTP dependencies (pyproject declares only openpyxl), and the thinner the Task Scheduler resident process,
the more reliable it is.
"""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_TIMEOUT = 20
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


@dataclass
class HttpResponse:
    status: int | None          # None = transport-layer failure (no HTTP response obtained)
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None    # transport-layer error description (DNS/timeout/connection refused etc.)


class HttpTransport(Protocol):
    def request(self, url: str, method: str = "GET") -> HttpResponse:
        """Issue one request; the implementation is responsible for timeout and UA, and must not throw uncaught exceptions."""
        ...


class UrllibTransport:
    """stdlib implementation: HEAD preferred over GET; all network exceptions are converged into HttpResponse(error=...)."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT, user_agent: str = DEFAULT_UA):
        self.timeout = timeout
        self.user_agent = user_agent

    def request(self, url: str, method: str = "GET") -> HttpResponse:
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": self.user_agent,
                                              "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = ""
                if method != "HEAD":
                    body = resp.read(262144).decode("utf-8", errors="replace")
                return HttpResponse(status=resp.status, body=body,
                                    headers=dict(resp.headers.items()))
        except urllib.error.HTTPError as e:
            # HTTP error codes are also valid responses (403/404/5xx are interpreted by the classifier)
            body = ""
            try:
                body = e.read(65536).decode("utf-8", errors="replace")
            except Exception:
                pass
            return HttpResponse(status=e.code, body=body, headers=dict(e.headers.items()))
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError,
                ValueError, http.client.HTTPException) as e:
            # ValueError: an illegal URL (e.g. a dirty link containing spaces/annotations in a cell) is also
            # converged into a result, and never throws to blow up the resident task — every probe must
            # land one record
            return HttpResponse(status=None, error=f"{type(e).__name__}: {e}")


class FakeTransport:
    """Testing/replay: looks up the table by (url, method) and returns a prefabricated response; a miss returns an explicit error."""

    def __init__(self, table: dict[tuple[str, str], HttpResponse]):
        self._table = {(u, m.upper()): r for (u, m), r in table.items()}
        self.calls: list[tuple[str, str]] = []

    def request(self, url: str, method: str = "GET") -> HttpResponse:
        self.calls.append((url, method))
        key = (url, method.upper())
        if key in self._table:
            return self._table[key]
        return HttpResponse(status=None, error=f"FakeTransport: no fixture for {key}")
