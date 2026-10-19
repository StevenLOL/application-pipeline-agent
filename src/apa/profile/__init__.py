"""R0 personal profile: absorb user documents -> single source of truth (docs/19 §4).


Users can also edit and update manually; **users must never be made to hand-fill
forms**.

Module split (parsing / extraction / merging strictly separated, each unit-testable):

- ``models``   — Claim (a fact sentence carrying provenance) / Profile / WorkAuthorization
- ``loader``   — docx/pdf/txt/md/json parsing (**zero LLM, zero network**)
- ``extract``  — candidate discovery (DSPy) + **code-layer verbatim location** (a
  candidate the model cannot locate is dropped)
- ``merge``    — three iron rules: user correction > newly extracted / refinement >
  replacement / conflicts are never silent
- ``store``    — profile.json snapshot + profile_history.jsonl change history (append-only)
"""

from .loader import Document, Segment, UnsupportedDocument, parse_dir, parse_document
from .merge import (MergeOutcome, apply_manual_edit, confirm_claim, mark_needs_human,
                    materialize_experiences, merge_claims, prune_drafts, resolve_conflict)
from .models import (Claim, ClaimStatus, DocFormat, Experience, Profile, Provenance,
                     WorkAuthorization)
from .store import history, load, record_change, review_queue, save, summarize

__all__ = [
    "Claim", "ClaimStatus", "DocFormat", "Document", "Experience", "MergeOutcome",
    "Profile", "Provenance", "Segment", "UnsupportedDocument", "WorkAuthorization",
    "apply_manual_edit", "confirm_claim", "history", "load", "mark_needs_human",
    "materialize_experiences", "merge_claims", "parse_dir", "parse_document",
    "prune_drafts", "record_change", "resolve_conflict", "review_queue", "save",
    "summarize",
]
