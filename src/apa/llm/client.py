"""APA unified LLM client (built in D8, extended in D9): four backends, user-selectable + automatic priority chain.

Architecture (technology-selection report + design decision: "prefer WorkBuddy's
own LLM for testing; the user can choose"):

    workbuddy ──► cloud ──► local
    (first priority) (opt-in) (fallback)

- **workbuddy**: the model built into the WorkBuddy desktop app (GLM-5.x /
  DeepSeek-V4 / Kimi-K3 / MiniMax-M3), exposed as a local OpenAI-compatible
  endpoint (127.0.0.1:8787) through the `workbuddy-connect-api` proxy. First
  priority for the user: zero extra cost for testing/dev iteration, and
  stronger than the local 1.5B (the measured list-extraction boundary is
  thereby broken through).
  Warning: **data-boundary discipline (honesty)**: requests go to WorkBuddy's
  cloud models - "this is essentially no different from using a cloud API
  directly" (upstream community docs). Therefore **workbuddy is on par with
  cloud: real submission materials must pass the Presidio gate (D16)** and
  must not be mistaken for "data never leaves the domain" just because "the
  proxy is on the local loopback".
- **cloud**: cloud-hosted private LLM (Bedrock `ap-southeast-1` In-Region is
  the primary candidate) - opt-in double switch + Presidio gate + In-Region
  discipline.
- **local**: llama.cpp server (8080) - fallback: data truly stays in-domain,
  but capability is limited (measured: 1.5B single-value extraction works,
  list extraction does not).

Selection:
- Explicit: `APA_LLM_BACKEND=workbuddy|cloud|local` (user-selectable, highest priority)
- Automatic (default): probe along the priority chain - workbuddy (8787 /healthz)
  -> cloud (only if the opt-in double switch holds) -> local (8080 /health);
  if none is available, fail loud with the probe results listed.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

BACKEND_WORKBUDDY = "workbuddy"
BACKEND_CLOUD = "cloud"
BACKEND_LOCAL = "local"

DEFAULT_WORKBUDDY_BASE_URL = "http://127.0.0.1:8787/v1"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_LOCAL_MODEL = "qwen2.5-1.5b-instruct"

# Backend priority chain (auto mode)
_PRIORITY = (BACKEND_WORKBUDDY, BACKEND_CLOUD, BACKEND_LOCAL)
# Scope of the material gate: backends whose requests leave the domain
# (workbuddy goes via the cloud, cloud connects to the cloud directly)
_EGRESS_BACKENDS = (BACKEND_WORKBUDDY, BACKEND_CLOUD)


@dataclass(frozen=True)
class LLMSettings:
    """Resolved LLM configuration (env is the single source, easy to inject in tests)."""

    backend: str
    base_url: str
    api_key: str
    model: str
    cloud_allowed: bool

    @property
    def is_egress(self) -> bool:
        """Whether the request leaves the domain (determines the scope of the material gate)."""
        return self.backend in _EGRESS_BACKENDS

    def describe(self) -> str:
        return (
            f"backend={self.backend} model={self.model} "
            f"base_url={self.base_url} cloud_allowed={self.cloud_allowed}"
        )


class CloudNotAllowed(RuntimeError):
    """Cloud backend not explicitly opted in - first layer of the double switch."""


class NoBackendAvailable(RuntimeError):
    """No usable backend in auto mode (fail-loud, with probe results attached)."""


class PresidioGateRequired(RuntimeError):
    """Real materials reaching an egress backend must pass the Presidio gate (intercepted before D16, fail-closed)."""


def _probe_one(base_url: str, path: str, timeout: float) -> bool:
    try:
        req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500     # 401/403 = the service is up, it just needs auth
    except (urllib.error.URLError, OSError, ValueError):
        return False


def probe_endpoint(base_url: str, timeout: float = 0.75) -> bool:
    """Probe whether an OpenAI-compatible endpoint is alive (health/models: any 200/401/403 counts as up).

    **No proxy** (the probe target is the local loopback / intranet endpoint;
    going through a proxy would hang the connection until timeout),
    **three paths in parallel** (the sandbox network layer hangs connect on
    closed ports until full timeout - real machines reject closed ports
    immediately; parallel + short timeout makes both environments fast).
    Measured 2026-10-19: the settings health check took 37.7s before the fix,
    <1s after.
    """
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        hits = list(pool.map(
            lambda p: _probe_one(base_url, p, timeout),
            ("/health", "/healthz", "/v1/models")))
    return any(hits)


def load_settings(
    *,
    backend: Optional[str] = None,
    allow_cloud: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    probe: Optional[Any] = None,
) -> LLMSettings:
    """Resolve LLM configuration from env (arguments can override, for tests).

    backend resolution: explicit argument > env ``APA_LLM_BACKEND`` > automatic priority chain (probing).
    ``probe`` can inject a probe function (for tests); defaults to ``probe_endpoint``.
    """
    e = os.environ if env is None else env
    probe_fn = probe or probe_endpoint
    cloud_allowed = (allow_cloud if allow_cloud is not None
                     else e.get("APA_ALLOW_CLOUD_LLM", "")) == "1"
    backend = (backend or e.get("APA_LLM_BACKEND") or "auto").lower()

    if backend == "auto":
        notes: list[str] = []
        # 1. workbuddy proxy (first priority)
        wb_url = e.get("APA_LLM_BASE_URL") or DEFAULT_WORKBUDDY_BASE_URL
        if probe_fn(wb_url):
            backend = BACKEND_WORKBUDDY
        else:
            notes.append(f"workbuddy: {wb_url} not up")
        # 2. cloud (only if opted in - not probed proactively, to avoid accidentally triggering cloud billing)
        if backend == "auto" and cloud_allowed:
            backend = BACKEND_CLOUD
        elif backend == "auto":
            notes.append("cloud: not opted in (APA_ALLOW_CLOUD_LLM!=1)")
        # 3. local (fallback)
        if backend == "auto":
            local_url = e.get("APA_LLM_BASE_URL") or DEFAULT_LOCAL_BASE_URL
            if probe_fn(local_url):
                backend = BACKEND_LOCAL
            else:
                notes.append(f"local: {local_url} not up")
        if backend == "auto":
            raise NoBackendAvailable(
                "no usable backend in auto mode (probed along the priority chain): "
                + "; ".join(notes)
                + ". Options: start workbuddy-connect-api (8787) / llama.cpp (8080), "
                "or set APA_ALLOW_CLOUD_LLM=1 and configure a cloud endpoint"
            )

    if backend not in (BACKEND_WORKBUDDY, BACKEND_CLOUD, BACKEND_LOCAL):
        raise ValueError(f"unknown APA_LLM_BACKEND: {backend!r} "
                         f"(auto|workbuddy|cloud|local)")
    if backend == BACKEND_CLOUD and not cloud_allowed:
        raise CloudNotAllowed(
            "APA_LLM_BACKEND=cloud requires APA_ALLOW_CLOUD_LLM=1 "
            "(data-sovereignty double switch; real materials also need the "
            "Presidio gate from D16)"
        )

    defaults = {
        BACKEND_WORKBUDDY: (DEFAULT_WORKBUDDY_BASE_URL, "auto"),
        BACKEND_LOCAL: (DEFAULT_LOCAL_BASE_URL, DEFAULT_LOCAL_MODEL),
        BACKEND_CLOUD: ("", ""),
    }
    base_url = e.get("APA_LLM_BASE_URL") or defaults[backend][0]
    model = e.get("APA_LLM_MODEL") or defaults[backend][1]
    api_key = e.get("APA_LLM_API_KEY") or ("EMPTY" if backend == BACKEND_LOCAL else "")
    return LLMSettings(backend=backend, base_url=base_url, api_key=api_key,
                       model=model, cloud_allowed=cloud_allowed)


def assert_material_cleared(material: str, *, cleared: bool) -> None:
    """Egress-backend material gate assertion (fail-closed placeholder until the D16 Presidio gate lands).

    Convention: callers must first run Presidio sanitization on **real submission
    materials** (D16) and pass ``cleared=True``; synthetic/public materials are
    likewise released with cleared=True (no PII risk). workbuddy is on par with
    cloud (both send requests out of the domain).
    """
    if not cleared:
        raise PresidioGateRequired(
            "egress backend requires material to pass the PII gate first "
            "(D16 Presidio); pass cleared=True only after sanitization"
        )


def build_client(settings: Optional[LLMSettings] = None, **overrides: Any):
    """Build an OpenAI-compatible client (workbuddy proxy / llama.cpp / Bedrock / Azure, all isomorphic)."""
    if settings is None:
        settings = load_settings(**overrides)
    from openai import OpenAI

    if not settings.base_url:
        raise RuntimeError(f"{settings.backend} backend requires APA_LLM_BASE_URL")
    if settings.backend != BACKEND_LOCAL and not settings.api_key:
        raise RuntimeError(
            f"{settings.backend} backend requires APA_LLM_API_KEY "
            "(workbuddy proxy: any non-empty key; Bedrock: aws-bedrock-token-generator "
            "derived Bearer; Azure: key or Entra ID)"
        )
    return OpenAI(base_url=settings.base_url, api_key=settings.api_key)


def chat(
    prompt: str,
    *,
    settings: Optional[LLMSettings] = None,
    system: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    json_mode: bool = False,
    material_cleared: bool = True,
    _client: Optional[Any] = None,
) -> str:
    """One chat completion (unified entry point).

    ``material_cleared``: PII-gate semantics for egress backends
    (workbuddy/cloud) - True = the caller declares the material already
    handled (synthetic/public materials); False = **must run the Presidio
    gate** (R3: on detection, raise - never silently release or sanitize).
    ``_client``: test seam (canary-leak tests inject a spy client).
    """
    if settings is None:
        settings = load_settings()
    if settings.is_egress and not material_cleared:
        from ..privacy import PIIGate

        PIIGate().assert_safe(prompt, label="outbound material")
    client = _client if _client is not None else build_client(settings)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    kwargs: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""
