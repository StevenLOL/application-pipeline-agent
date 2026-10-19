"""F3 extraction signature set (stage 9): JD source text to structured facts.

Three extraction targets (staged development plan, stage 9): requirements / deadline / assessment process.
Shared discipline (the extraction-layer defense for accident 4):
- Extract only what the JD **explicitly states**; do not infer, do not complete, do not fill in with generic
  template processes;
- Every requirement item carries a **verbatim quote from the source text** (quotes are machine-checkable —
  the same idea as A4 claim-verification);
- "the page does not publish a deadline" is a first-class state (NOT_PUBLISHED), not an empty value — the
  extraction-layer countermeasure for evergreen roles' sparse deadlines (a red-level issue in the design docs).

DSPy 3.x usage: `dspy.Predict(Signature)`; the LM is configured uniformly via `apa.llm`
(local llama.cpp by default / cloud private opt-in).
"""

from __future__ import annotations

import dspy


class ExtractRequirements(dspy.Signature):
    """Extract a list of hard requirements from the JD source text. Extract only requirements the JD explicitly states; do not infer, do not complete."""

    jd_text: str = dspy.InputField(desc="Full JD source text (including the Requirements/Preferred sections)")
    requirements: str = dspy.OutputField(
        desc=(
            "A JSON array string, each item {\"category\": \"must|preferred|culture\", "
            "\"quote\": \"<verbatim fragment of the JD source text>\"}. The quote must be verbatim from the "
            "source text; items the JD does not explicitly state are not extracted at all."
        )
    )


class ExtractDeadline(dspy.Signature):
    """Extract the submission deadline from the JD source text. An unpublished page is a valid state; speculation is forbidden.

    Measured in stage 9 (Qwen2.5-1.5B): when the desc only says "speculation forbidden", the small model
    places the conservative instruction above the extraction instruction — even when the JD explicitly
    writes "Closing on 22 Oct 2026" it still outputs NOT_PUBLISHED.
    → The instruction was changed to be **explicit in both directions**: when the text has a clear deadline
    statement, its source-text date must be output; only when it is entirely unmentioned output
    NOT_PUBLISHED. Format conversion is done at the code layer (the model extracts the source text,
    the code normalizes — the same origin as the A4 entity normalization).
    """

    jd_text: str = dspy.InputField(desc="Full JD source text")
    deadline: str = dspy.OutputField(
        desc=(
            "The deadline. Rules: ① when the text contains a clear deadline statement such as "
            "'Closing on <date>' / 'Apply by <date>', the source-text date **must** be output "
            "(e.g. '22 Oct 2026'), not NOT_PUBLISHED; ② only when the deadline is entirely unmentioned "
            "output NOT_PUBLISHED."
        )
    )


class ExtractAssessment(dspy.Signature):
    """Discover the names of assessment/exam stages in the application process from the JD source text. An empty array if none.

    Accident-4 defense: checklists/generators splice **generic template processes** such as
    "CivicTech Digital Academy online technical assessment" into roles the JD never mentions — the extraction layer must
    recognize only assessment stages that appear in the JD source text, returning [] when unmentioned.

    Measured in stage 9 (1.5B): making the model do the triple task of "location + verbatim quote + JSON"
    at once reliably escapes (Contoso explicitly writes Digital Academy yet still returns []) → the division of
    labor was restructured: **the model only discovers candidate names (a single-value list), and the quote
    is located in the source text by the code layer** (if it cannot be located it is dropped — assessment
    items without source-text support are not trusted; this is exactly the mechanization of the accident-4
    defense).
    """

    jd_text: str = dspy.InputField(desc="Full JD source text")
    assessments: str = dspy.OutputField(
        desc=(
            "A JSON array string, each item being an assessment stage name. "
            "Rules: ① when the text explicitly mentions an assessment stage such as assessment / exam / "
            "technical assessment, its name **must** be listed; ② only when no assessment stage is mentioned "
            "at all return []. "
            "Example: when the JD contains \"Pass the online technical assessment administered by the "
            "Civic Technology Agency's Digital Academy\", output "
            "[\"CivicTech Digital Academy online technical assessment\"]; "
            "when the JD text contains no occurrence of the word assessment, output []."
        )
    )


ALL = (ExtractRequirements, ExtractDeadline, ExtractAssessment)
