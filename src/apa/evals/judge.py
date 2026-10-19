"""APA semantic-evaluation judge abstraction (D6 / F4 evaluation gate).

Three judge tiers, data sovereignty first:
- ``replay`` (default, CI): deterministic replay of pre-recorded verdicts.
  It validates the **gate logic and case-metric mapping**, not "an AI judged
  this" - verdicts come from human adjudication (case facts established by
  the rule engine + git archaeology), formalised into the DeepEval protocol,
  to be re-run and replaced once the local judge is online.
- ``llamacpp`` (local live): OpenAI-compatible endpoint (llama.cpp server,
  the D8 asset).
- ``openai`` (cloud): available only behind an explicit opt-in; real
  materials must pass the Presidio blocking gate first.

Canon 1 (docs/04 G2, stated by the Ragas docs, full text in README):
1. the judge model must be >= the system under test;
2. scores are noisy across runs of the same pipeline -> bootstrap CI is not
   optional;
3. billing is per claim -> there is a cost.
How this module implements them: replay mode does not pose as a judge (the
boundary is written into the report); bootstrap CI is enabled at the R1
ablation; every live judge run records model/version/config into
reports/evals/run_*/judge_config.md.

DeepEval 4.3.1 FaithfulnessMetric judge-call stages (anchored on prompt
template literals, see site-packages/deepeval/metrics/faithfulness/templates/):
- truths stage: contains "FACTUAL, undisputed truths" + "Text:"
  (material = retrieval_context)
- claims stage: contains "FACTUAL, undisputed truths" + "AI Output:"
  (material = actual_output) - same-source wording as the truths stage; they
  MUST be disambiguated by the "AI Output:" anchor (a measured pitfall)
- verdicts stage: contains "Retrieval Contexts:" (batch-processes all claims)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from deepeval.models.base_model import DeepEvalBaseLLM

# Semantic case data (cases + pre-recorded replay verdicts), self-contained
# judge.py lives at <repo>/src/apa/evals/ -> parents[3] = repo root
CASES_PATH = Path(__file__).resolve().parents[3] / "evals" / "deepeval" / "cases.json"

DEFAULT_THRESHOLD = 0.7


class ReplayMiss(Exception):
    """A pre-recorded verdict matched no case - fail loud, silent fallback forbidden.

    A silent fallback in CI would make the gate appear to work (arbitrary
    scores that make assertions vacuously true/false), so every miss raises,
    forcing a re-record instead of a pass-through.
    """


def _norm(text: str) -> str:
    return " ".join(text.split())


class ReplayJudge(DeepEvalBaseLLM):
    """Deterministic replay judge: matches pre-recorded verdicts by prompt
    stage + case keywords.

    Boundary (honesty principle): this judge is **not an LLM**. Recorded
    verdicts come from human adjudication - case facts were established
    three ways (rule engine + git archaeology + review citations) and
    formalised into the DeepEval protocol. CI uses it to validate the gate
    logic (threshold semantics, case-metric mapping, catch/pass behaviour);
    once the local judge is online it re-runs the same protocol and replaces
    this.
    """

    def __init__(self, cases_path: Path = CASES_PATH):
        self._cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]

    def load_model(self):
        return self

    def get_model_name(self) -> str:
        return "replay-judge (deterministic CI; human-adjudicated, not an LLM)"

    @staticmethod
    def _stage(prompt: str) -> str:
        if "Retrieval Contexts:" in prompt:
            return "verdicts"
        if "AI Output:" in prompt:
            return "claims"
        if "Below is a list of Contradictions" in prompt:
            # generate_reason template: the overall reason narrative. The gate
            # only asserts score; the reason text is covered by a shared rule
            # (see cases.json _shared-reason).
            return "reason"
        if "FACTUAL, undisputed truths" in prompt:
            return "truths"
        raise ReplayMiss(
            "replay judge: prompt matches no known faithfulness stage "
            "(anchors: 'Retrieval Contexts:' / 'AI Output:' / 'Below is a list of Contradictions' "
            "/ 'FACTUAL, undisputed truths'); deepeval template may have changed — update the router"
        )

    def _match(self, prompt: str) -> tuple[dict[str, Any], str]:
        p = _norm(prompt)
        stage = self._stage(p)
        for case in self._cases:
            rule = case.get("replay")
            if not rule or stage not in rule:
                continue
            if all(kw in p for kw in rule[stage]["match_all"]):
                return case, stage
        raise ReplayMiss(
            f"replay judge: no case matched stage={stage} prompt head: {p[:120]} "
            "— add/extend the case in evals/deepeval/cases.json"
        )

    def generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        case, stage = self._match(prompt)
        return json.dumps(case["replay"][stage]["payload"])

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        return self.generate(prompt, schema)

    def supports_json_mode(self) -> bool:
        return True

    def supports_multimodal(self) -> bool:
        return False


class LlamacppJudge(DeepEvalBaseLLM):
    """Local llama.cpp server (OpenAI-compatible) judge.

    Config via env: ``APA_JUDGE_BASE_URL`` (e.g. http://127.0.0.1:8080/v1),
    ``APA_JUDGE_MODEL``, ``APA_JUDGE_API_KEY`` (any non-empty value locally).
    Canon-1 clause 1 implementation: the model name goes into the report and
    a human verifies capability >= the system under test.
    """

    def __init__(self) -> None:
        base_url = os.environ.get("APA_JUDGE_BASE_URL")
        if not base_url:
            raise RuntimeError(
                "llamacpp judge selected but APA_JUDGE_BASE_URL is not set "
                "(expected a local llama.cpp server OpenAI-compatible endpoint)"
            )
        self._model = os.environ.get("APA_JUDGE_MODEL", "local-gguf")
        self._client = None

    def load_model(self):
        from openai import OpenAI  # deepeval dependency already installed

        self._client = OpenAI(
            base_url=os.environ["APA_JUDGE_BASE_URL"],
            api_key=os.environ.get("APA_JUDGE_API_KEY", "EMPTY"),
        )
        return self

    def get_model_name(self) -> str:
        return f"llamacpp:{self._model}@{os.environ.get('APA_JUDGE_BASE_URL', '?')}"

    def generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        if self._client is None:
            self.load_model()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        if self._client is None:
            self.load_model()
        from openai import AsyncOpenAI

        aclient = AsyncOpenAI(
            base_url=os.environ["APA_JUDGE_BASE_URL"],
            api_key=os.environ.get("APA_JUDGE_API_KEY", "EMPTY"),
        )
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await aclient.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    def supports_json_mode(self) -> bool:
        return True

    def supports_multimodal(self) -> bool:
        return False


class OpenaiJudge(DeepEvalBaseLLM):
    """Cloud judge - explicit opt-in (``APA_JUDGE_MODE=openai`` +
    ``APA_ALLOW_CLOUD_JUDGE=1``).

    Data-sovereignty boundary: real submission materials must pass the
    Presidio blocking gate before reaching this judge (D16 scope); this class
    is for synthetic/public materials or explicitly authorised use.
    """

    def __init__(self) -> None:
        if os.environ.get("APA_ALLOW_CLOUD_JUDGE") != "1":
            raise RuntimeError(
                "cloud judge requires explicit opt-in: set APA_ALLOW_CLOUD_JUDGE=1 "
                "(data-sovereignty boundary; real materials must pass the Presidio gate first)"
            )
        self._model = os.environ.get("APA_JUDGE_MODEL", "gpt-4o-mini")
        self._client = None

    def load_model(self):
        from openai import OpenAI

        self._client = OpenAI()  # OPENAI_API_KEY
        return self

    def get_model_name(self) -> str:
        return f"openai:{self._model}"

    def generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        if self._client is None:
            self.load_model()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> str:
        if self._client is None:
            self.load_model()
        from openai import AsyncOpenAI

        aclient = AsyncOpenAI()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await aclient.chat.completions.create(**kwargs)
        return resp.choices[0].message.content

    def supports_json_mode(self) -> bool:
        return True

    def supports_multimodal(self) -> bool:
        return False


def build_judge(mode: Optional[str] = None) -> DeepEvalBaseLLM:
    """Build a judge by mode. Default replay (deterministic CI).

    mode source: argument > env ``APA_JUDGE_MODE`` > replay.
    """
    mode = (mode or os.environ.get("APA_JUDGE_MODE") or "replay").lower()
    if mode == "replay":
        return ReplayJudge()
    if mode == "llamacpp":
        return LlamacppJudge()
    if mode == "openai":
        return OpenaiJudge()
    raise ValueError(f"unknown judge mode: {mode!r} (replay|llamacpp|openai)")


def load_cases(cases_path: Path = CASES_PATH) -> list[dict[str, Any]]:
    return json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
