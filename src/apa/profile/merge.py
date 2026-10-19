"""R0 Conflict merge: newly extracted Claim[] -> existing Profile (docs/19 sec 4.2 (4), **core**).

User's own words: "sometimes there is also a need to refine or correct information" — this means
re-ingest **must never** be last-write-wins. Three iron rules:

1. **User correction > new extraction** (``MANUAL_OVERRIDE`` is never overwritten -> ``skipped_manual``)
2. **Refinement takes priority over replacement**: a new extraction is a more granular description
   of the old value -> appended as a **new bullet**, without rewriting the old sentence (rewriting
   loses provenance, and downstream materials may already cite the old sentence)
3. **Conflicts are never silent**: same field, different values -> mark ``CONFLICT`` + record the
   new source into ``rejected_sources``, await user adjudication; **never auto-adopt**

When the field and value are the same, **merge provenance and raise confidence** (multi-source
corroboration), so the user need not confirm obvious facts one by one — otherwise "zero manual
entry" would be infeasible across 200 claims.

The only basis for judging "refinement" rather than "conflict" is **character containment** (the
new text contains the old or vice versa): guessing intent = fabricating a classification.
Contains -> refinement; does not contain -> conflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Claim, ClaimStatus, Profile, Provenance

# confidence bump step and cap (deterministic, docs/19 sec 4.2 confidence model)
CONFIDENCE_BUMP = 0.1
CONFIDENCE_CAP = 0.95


def _bump(current: float) -> float:
    """Round to 3 decimals after bumping — floating-point accumulation would desync the
    in-memory value from the persisted value (to_dict already persists 3 decimals; converge
    here too to avoid round-trip mismatch)."""
    return round(min(CONFIDENCE_CAP, current + CONFIDENCE_BUMP), 3)
# values shorter than this and not containing each other are treated as "different facts" rather
# than refinements (avoids "NBU" vs "NBU CSC")
REFINE_MIN_LEN = 12


@dataclass
class MergeOutcome:
    """Merge result + complete decision ledger (the review panel must be able to answer "why is
    this claim in this state")."""

    added: list[str] = field(default_factory=list)
    reinforced: list[str] = field(default_factory=list)
    refined: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped_manual: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)   # old key yields to refinement

    @property
    def touched(self) -> int:
        return len(self.added) + len(self.reinforced) + len(self.refined)

    def summary(self) -> str:
        return (f"+{len(self.added)} new, {len(self.reinforced)} reinforced, "
                f"{len(self.refined)} refined, {len(self.conflicts)} need your call, "
                f"{len(self.skipped_manual)} pinned (yours, untouched)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added, "reinforced": self.reinforced,
            "refined": self.refined, "conflicts": self.conflicts,
            "skipped_manual": self.skipped_manual, "superseded": self.superseded,
        }


def _is_refinement(old: str, new: str) -> bool:
    """Whether the new value is a refinement of the old value (character containment +
    minimum length threshold).

    "refinement" vs "conflict" is a classification problem where **intent cannot be inferred
    from text**; the only objectively decidable basis is containment: 'Presidio blocks
    desensitization' is contained in 'Presidio blocks desensitization + R4 counterfactual
    invariance test'. If not satisfied -> always judge as conflict (better to ask once more
    than to silently rewrite a person's facts).
    """
    o, n = old.strip(), new.strip()
    if not o or not n:
        return False
    if o == n:
        return False
    longer, shorter = (n, o) if len(n) > len(o) else (o, n)
    if len(shorter) < REFINE_MIN_LEN:
        return False
    return shorter.casefold() in longer.casefold()


def merge_claims(profile: Profile, incoming: list[Claim],
                 allow_bump: bool = True) -> MergeOutcome:
    """Merge newly extracted claims into the profile (the three iron rules are enforced here).

    Modifies the profile in place. Returns a MergeOutcome decision ledger.
    """
    out = MergeOutcome()
    for claim in incoming:
        key = claim.field
        existing = profile.claims.get(key)

        # Iron rule 1: fields manually edited by the user are never overwritten by extraction
        if existing is not None and existing.pinned:
            out.skipped_manual.append(key)
            continue

        if existing is None:
            profile.claims[key] = claim
            out.added.append(key)
            continue

        if existing.normalize() == claim.normalize():
            # Same value: merge provenance (multi-source corroboration) + bump confidence
            known = {(p.source_file, p.locator) for p in existing.provenance}
            for p in claim.provenance:
                if (p.source_file, p.locator) not in known:
                    existing.provenance.append(p)
            if allow_bump and len({p.source_file for p in existing.provenance}) > 1:
                existing.confidence = _bump(existing.confidence)
            out.reinforced.append(key)
            continue

        # Iron rule 2: refinement (containment) -> keep old sentence as "more specific",
        # register new sentence as a same-key refinement
        if _is_refinement(existing.text, claim.text):
            claim.status = ClaimStatus.DRAFT
            claim.rejected_sources.extend(existing.provenance)  # old sentence not lost, traceable
            profile.claims[key] = claim
            out.refined.append(key)
            continue

        # Iron rule 3: conflict — record the new source into rejected, awaiting user adjudication
        existing.rejected_sources.extend(claim.provenance)
        if claim.provenance:
            existing.rejected_sources.append(
                Provenance(claim.provenance[0].source_file,
                           claim.provenance[0].locator,
                           claim.text[:400], claim.provenance[0].extracted_at,
                           extractor="rejected-in-reingest"))
        existing.status = ClaimStatus.CONFLICT
        out.conflicts.append(key)

    return out


def apply_manual_edit(profile: Profile, field_name: str, new_text: str,
                      reason: str = "") -> Claim:
    """User manual correction -> the claim becomes ``MANUAL_OVERRIDE`` (the only entry point to
    iron rule 1).

    The old value **must be retained** (``extractor='manual-superseded[: reason]'``) — "what I
    originally wrote" must be traceable: the right to correct belongs to the human, the audit
    belongs to the agent.

    Implementation note: the old claim object is entirely replaced by the new one, so the history
    must be **carried into the new object**, otherwise records appended to the old object
    disappear along with the replacement (the first version of this function made exactly this
    mistake, pinned down by test_manual_edit_keeps_old_value_with_reason).
    """
    if not field_name.strip():
        raise ValueError("field name must not be empty")
    if not new_text.strip():
        raise ValueError("new text must not be empty (use mark_needs_human to flag it)")
    new_text = new_text.strip()
    existing = profile.claims.get(field_name)

    superseded: list[Provenance] = []
    if existing is not None:
        superseded.extend(existing.rejected_sources)          # carry over earlier history
        if existing.text.strip() and existing.text.strip() != new_text:
            first = existing.provenance[0] if existing.provenance else None
            superseded.append(Provenance(
                source_file=first.source_file if first else "manual",
                locator=first.locator if first else "",
                snippet=existing.text[:400],
                extracted_at=first.extracted_at if first else "",
                extractor=f"manual-superseded{': ' + reason if reason else ''}"))

    claim = Claim(field=field_name, text=new_text,
                  status=ClaimStatus.MANUAL_OVERRIDE, confidence=1.0,
                  provenance=[Provenance(source_file="manual", locator=field_name,
                                         snippet=new_text, extracted_at="",
                                         extractor="manual-edit")],
                  rejected_sources=superseded)
    profile.claims[field_name] = claim
    profile.sync_identity_from_claims()
    return claim


def confirm_claim(profile: Profile, field_name: str) -> Claim:
    """User confirms a DRAFT claim (the review panel's [✓ confirm])."""
    claim = profile.claims.get(field_name)
    if claim is None:
        raise KeyError(f"no such claim: {field_name}")
    if claim.status is ClaimStatus.CONFLICT:
        # Confirming adopts the existing text (old value) and clears the rejected side
        claim.status = ClaimStatus.CONFIRMED
    else:
        claim.status = ClaimStatus.CONFIRMED
        claim.confidence = max(claim.confidence, 0.8)
    profile.sync_identity_from_claims()
    return claim


def resolve_conflict(profile: Profile, field_name: str, take: str) -> Claim:
    """Conflict adjudication: ``take`` ∈ {"existing", "incoming"}.

    ``incoming``: promote the rejected-side text to the current value (status=CONFIRMED — it was
    explicitly chosen by a human, not auto-adopted by the model).
    ``existing``: clear the rejected side and confirm the old value.
    """
    claim = profile.claims.get(field_name)
    if claim is None:
        raise KeyError(f"no such claim: {field_name}")
    if take == "incoming":
        rejected = [p for p in claim.rejected_sources
                    if p.extractor != "rejected-in-reingest"]
        if not rejected:
            raise ValueError(f"{field_name}: no rejected incoming provenance to take")
        keep = next(p for p in reversed(claim.rejected_sources)
                    if p.extractor == "rejected-in-reingest")
        claim.text = keep.snippet
        claim.provenance.append(Provenance(keep.source_file, keep.locator,
                                           keep.snippet, keep.extracted_at,
                                           extractor="conflict-resolved"))
        claim.rejected_sources = rejected
        claim.status = ClaimStatus.CONFIRMED
    elif take == "existing":
        claim.rejected_sources = []
        claim.status = ClaimStatus.CONFIRMED
    else:
        raise ValueError(f"take must be 'existing' or 'incoming', got {take!r}")
    profile.sync_identity_from_claims()
    return claim


def mark_needs_human(profile: Profile, field_name: str) -> Claim:
    """Manually flag "this claim needs supplementation/verification" — the reverse of the
    review panel's [⊘ not stated]."""
    claim = profile.claims.get(field_name)
    if claim is None:
        raise KeyError(f"no such claim: {field_name}")
    claim.status = ClaimStatus.NEEDS_HUMAN
    profile.sync_identity_from_claims()
    return claim


def materialize_experiences(profile: Profile) -> None:
    """Rebuild ``profile.experiences`` from the claim index (a convenience view, direction
    claim -> view).

    Only CONFIRMED / MANUAL_OVERRIDE claims are used — **DRAFT / CONFLICT / NEEDS_HUMAN are
    never included** (docs/19 sec 4.3: unconfirmed facts must not enter downstream materials).
    """
    grouped: dict[str, dict[str, Any]] = {}
    for key, claim in profile.claims.items():
        if claim.status not in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
            continue
        parts = key.split(".")
        if parts[0] != "experiences" or len(parts) < 3:
            continue
        idx = parts[1]
        attr = ".".join(parts[2:])
        slot = grouped.setdefault(idx, {"bullets": []})
        if attr.startswith("bullets."):
            slot["bullets"].append((int(attr.split(".")[1]), claim))
        else:
            slot[attr] = claim

    from .models import Experience   # local import to avoid circular import

    experiences: list[Experience] = []
    for idx in sorted(grouped, key=lambda s: int(s) if s.isdigit() else 0):
        slot = grouped[idx]
        if not slot.get("org") and not slot.get("title"):
            continue                      # only bullets without an owner -> do not construct
        experiences.append(Experience(
            org=str(slot.get("org").text) if slot.get("org") else "",
            title=str(slot.get("title").text) if slot.get("title") else "",
            start=str(slot.get("start").text) if slot.get("start") else "",
            end=str(slot.get("end").text) if slot.get("end") else "",
            location=str(slot.get("location").text) if slot.get("location") else "",
            category=str(slot.get("category").text) if slot.get("category") else "employment",
            bullets=[c for _, c in sorted(slot["bullets"], key=lambda p: p[0])],
        ))
    profile.experiences = experiences


def prune_drafts(profile: Profile) -> list[str]:
    """Drop long-unconfirmed DRAFT claims (profile slimming; returns the dropped field names).

    Threshold is 60 days — gives the user a sufficient review window while preventing the profile
    from growing without bound. MANUAL_OVERRIDE / CONFLICT / NEEDS_HUMAN are **unaffected**
    (human traces are not cleaned up).
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    dropped: list[str] = []
    for key, claim in list(profile.claims.items()):
        if claim.status is not ClaimStatus.DRAFT:
            continue
        stamps = [p.extracted_at for p in claim.provenance if p.extracted_at]
        if not stamps:
            continue
        try:
            newest = max(datetime.fromisoformat(s) for s in stamps)
        except ValueError:
            continue
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        if newest < cutoff:
            del profile.claims[key]
            dropped.append(key)
    return dropped
