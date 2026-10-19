"""Unified LLM client unit tests (four-backend priority chain + dual switches
+ material gate).

Real network forbidden: probing goes through the injected probe function;
client construction only validates configuration.
"""

from __future__ import annotations

import pytest

from apa.llm import (
    BACKEND_CLOUD,
    BACKEND_LOCAL,
    BACKEND_WORKBUDDY,
    CloudNotAllowed,
    LLMSettings,
    NoBackendAvailable,
    PresidioGateRequired,
    assert_material_cleared,
    build_client,
    load_settings,
    probe_endpoint,
)

EMPTY_ENV: dict[str, str] = {}
UP = lambda url: True   # noqa: E731  (probe stub)
DOWN = lambda url = False: False  # noqa: E731


# ---- automatic priority chain (design decision: workbuddy → cloud → local) ----

def test_auto_prefers_workbuddy_when_up() -> None:
    """First priority: the workbuddy proxy (8787) is up → choose it."""
    s = load_settings(env=EMPTY_ENV, probe=UP)
    assert s.backend == BACKEND_WORKBUDDY
    assert s.base_url == "http://127.0.0.1:8787/v1"


def test_auto_falls_to_local_when_workbuddy_down() -> None:
    """workbuddy down, cloud not opted in, local up → local (fallback)."""
    def probe(url):
        return "8080" in url

    s = load_settings(env=EMPTY_ENV, probe=probe)
    assert s.backend == BACKEND_LOCAL
    assert s.base_url == "http://127.0.0.1:8080/v1"


def test_auto_falls_to_cloud_when_opted_in() -> None:
    """workbuddy down + cloud opt-in → cloud (skips the local probe)."""
    s = load_settings(
        env={"APA_ALLOW_CLOUD_LLM": "1",
             "APA_LLM_BASE_URL": "https://bedrock-runtime.ap-southeast-1.amazonaws.com/openai/v1",
             "APA_LLM_API_KEY": "derived-bearer",
             "APA_LLM_MODEL": "anthropic.claude-sonnet-5"},
        probe=DOWN,
    )
    assert s.backend == BACKEND_CLOUD
    assert "ap-southeast-1" in s.base_url  # In-Region discipline


def test_auto_none_available_fails_loud() -> None:
    """None available → fail-loud and list the probe results (no silent
    degradation)."""
    with pytest.raises(NoBackendAvailable, match="workbuddy.*local"):
        load_settings(env=EMPTY_ENV, probe=DOWN)


# ---- explicit selection takes priority over the automatic chain ----

def test_explicit_backend_overrides_chain() -> None:
    s = load_settings(backend=BACKEND_LOCAL, env=EMPTY_ENV, probe=UP)
    assert s.backend == BACKEND_LOCAL


def test_explicit_workbuddy_skips_probe() -> None:
    s = load_settings(backend=BACKEND_WORKBUDDY, env=EMPTY_ENV, probe=DOWN)
    assert s.backend == BACKEND_WORKBUDDY  # an explicit user choice skips probing (failures are visible to the caller)


def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="unknown APA_LLM_BACKEND"):
        load_settings(backend="grok", env=EMPTY_ENV, probe=UP)


# ---- dual switches and gate ----

def test_cloud_requires_allow_switch() -> None:
    with pytest.raises(CloudNotAllowed, match="APA_ALLOW_CLOUD_LLM=1"):
        load_settings(backend=BACKEND_CLOUD, env=EMPTY_ENV, probe=UP)


def test_workbuddy_is_egress_like_cloud() -> None:
    """Data-boundary discipline: workbuddy requests go through the cloud →
    same tier as cloud, not local."""
    s = load_settings(backend=BACKEND_WORKBUDDY, env=EMPTY_ENV, probe=UP)
    assert s.is_egress is True
    s2 = load_settings(backend=BACKEND_LOCAL, env=EMPTY_ENV, probe=UP)
    assert s2.is_egress is False


def test_material_gate_blocks_uncleared() -> None:
    with pytest.raises(PresidioGateRequired, match="PII gate"):
        assert_material_cleared("real CL", cleared=False)


def test_material_gate_passes_cleared() -> None:
    assert_material_cleared("synthetic", cleared=True)  # no raise means pass


# ---- client construction ----

def test_build_client_local() -> None:
    s = load_settings(backend=BACKEND_LOCAL, env=EMPTY_ENV, probe=UP)
    client = build_client(s)
    assert str(client.base_url).startswith("http://127.0.0.1:8080")


def test_build_client_requires_key_when_not_local() -> None:
    s = LLMSettings(backend=BACKEND_WORKBUDDY, base_url="http://127.0.0.1:8787/v1",
                    api_key="", model="auto", cloud_allowed=False)
    with pytest.raises(RuntimeError, match="APA_LLM_API_KEY"):
        build_client(s)


def test_chat_egress_blocks_uncleared_material_with_pii() -> None:
    """R3 upgraded semantics: uncleared and **PII detected** → the Presidio
    gate blocks it (the client is not even built)."""
    s = LLMSettings(
        backend=BACKEND_WORKBUDDY, base_url="http://127.0.0.1:8787/v1",
        api_key="k", model="auto", cloud_allowed=False,
    )
    from apa.llm import chat

    with pytest.raises(PresidioGateRequired):
        chat("my NRIC is S1234567D, email me at me@x.com",
             settings=s, material_cleared=False)


def test_chat_egress_passes_uncleared_but_clean_material() -> None:
    """R3 new semantics, positive side: uncleared but **no PII** → allowed
    (clean text is not a leak)."""
    s = LLMSettings(
        backend=BACKEND_WORKBUDDY, base_url="http://127.0.0.1:8787/v1",
        api_key="k", model="auto", cloud_allowed=False,
    )
    from types import SimpleNamespace

    from apa.llm import chat

    ok_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="ok"))]))))

    out = chat("pure technical text about RAG pipelines",
               settings=s, material_cleared=False, _client=ok_client)
    assert out == "ok"
