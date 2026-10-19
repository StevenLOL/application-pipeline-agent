"""APA unified LLM client layer (built in D8, four-backend chain in D9, FakeLM layered testing pre-staged in D11)."""

from .client import (
    BACKEND_CLOUD,
    BACKEND_LOCAL,
    BACKEND_WORKBUDDY,
    DEFAULT_LOCAL_BASE_URL,
    DEFAULT_LOCAL_MODEL,
    DEFAULT_WORKBUDDY_BASE_URL,
    CloudNotAllowed,
    LLMSettings,
    NoBackendAvailable,
    PresidioGateRequired,
    assert_material_cleared,
    build_client,
    chat,
    load_settings,
    probe_endpoint,
)
from .fake import FakeLM, FakePredictor

__all__ = [
    "BACKEND_CLOUD",
    "BACKEND_LOCAL",
    "BACKEND_WORKBUDDY",
    "DEFAULT_LOCAL_BASE_URL",
    "DEFAULT_LOCAL_MODEL",
    "DEFAULT_WORKBUDDY_BASE_URL",
    "CloudNotAllowed",
    "FakeLM",
    "FakePredictor",
    "LLMSettings",
    "NoBackendAvailable",
    "PresidioGateRequired",
    "assert_material_cleared",
    "build_client",
    "chat",
    "load_settings",
    "probe_endpoint",
]
