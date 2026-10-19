"""FakeLM (pre-staged in D11): a DSPy LM stand-in with preset responses -
deterministic, zero network at the unit layer.

Industry equivalent: LangChain's `FakeListChatModel` (returns predefined
responses in order).
Rule: mock the interface, not the core inference logic - what is under test is
APA's deterministic mechanisms (schema validation / agreement rate /
needs_human); model quality belongs to the evaluation layer.
"""

from __future__ import annotations

from typing import Any, Optional

import dspy


class FakeLM(dspy.BaseLM):
    """A DSPy LM stand-in that returns preset responses in order (or by match).

    Subclasses dspy.BaseLM (DSPy 3.x requires LM to be a BaseLM subclass;
    duck typing is rejected).
    Two modes:
    - ``responses`` as a list: popped in call order (LangChain FakeListChatModel
      semantics); once exhausted, repeat the last item (configurable with
      ``cycle=False`` to raise instead);
    - ``responses`` as a dict: matched and returned by "substring contained in
      the prompt" (in insertion order; first hit wins).

    Usage:
        fake = FakeLM(responses=['{"deadline": "2026-10-22"}'] * 5)
        dspy.configure(lm=fake)   # or JDExtractor(lm=fake)
    """

    def __init__(
        self,
        responses: list[str] | dict[str, str],
        *,
        cycle: bool = False,
        model_name: str = "fake-lm",
    ):
        super().__init__(model=model_name)
        self._queue = list(responses) if isinstance(responses, list) else None
        self._mapped = dict(responses) if isinstance(responses, dict) else None
        self._cycle = cycle
        self._i = 0
        self.calls: list[dict[str, Any]] = []  # audit: the messages of each call
        self._name = model_name

    def __call__(self, messages: Optional[Any] = None, prompt: Optional[str] = None, **kwargs: Any) -> list[str]:
        self.calls.append({"messages": messages, "prompt": prompt, **kwargs})
        if self._queue is not None:
            if self._i < len(self._queue):
                out = self._queue[self._i]
                self._i += 1
                return [out]
            if self._cycle and self._queue:
                return [self._queue[-1]]
            raise AssertionError(
                f"FakeLM: preset responses exhausted ({len(self._queue)} used up) - "
                "extend `responses` when the test needs more"
            )
        # dict mode: match by substring
        text = prompt or str(messages)
        for needle, resp in self._mapped.items():
            if needle in text:
                return [resp]
        raise AssertionError(
            f"FakeLM: no matching rule for the prompt (match keys: {list(self._mapped)}) - "
            "add keys or change the prompt"
        )

    def get_model_name(self) -> str:
        return self._name


class FakePredictor:
    """Directly simulate the call interface of `dspy.Predict(Signature)` (bypassing the DSPy Adapter).

    DSPy's Predict goes through the Adapter (Chat/JSON), which re-parses the LM
    reply into output fields - a test stand-in fed raw text would be
    second-parsed by the Adapter. FakePredictor simulates the predictor call
    interface directly (`__call__(jd_text=...)` -> a result object with output
    field attributes), letting extractor-mechanism tests focus 100% on
    deterministic code (mock the interface, not the mechanism).

    Usage (injected via JDExtractor.set_predictor):
        ex = JDExtractor(lm=FakeLM(responses=[]))
        ex.set_predictor("deadline", FakePredictor("deadline", ['{"deadline": "22 Oct 2026"}'] * 5))
    """

    def __init__(self, field: str, outputs: list[str]):
        self.field = field
        self.outputs = list(outputs)
        self._i = 0
        self.calls: list[str] = []

    def __call__(self, **kwargs: Any):
        self.calls.append(str(kwargs.get("jd_text", ""))[:200])
        if self._i >= len(self.outputs):
            raise AssertionError(
                f"FakePredictor[{self.field}]: preset outputs exhausted ({len(self.outputs)}) - extend `outputs`"
            )
        from types import SimpleNamespace

        out = self.outputs[self._i]
        self._i += 1
        return SimpleNamespace(**{self.field: out})
