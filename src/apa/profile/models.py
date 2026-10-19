"""R0 Personal profile data model: a single source of truth with provenance (docs/19 sec 4.1).


There is also a need to refine or correct information, and here we should absorb the user's docx,
pdf, txt, or md documents; the user can also manually edit and update — **we must not make the
user fill things in by hand**".

Three design iron rules (the model foundation for docs/19 sec 4.2 merge's three iron rules):
1. **User correction > new extraction**: a ``ClaimStatus.MANUAL_OVERRIDE`` claim is never
   overwritten by re-ingest — this is where "refinement/correction needs" land.
2. **Claims are first-class citizens**: every fact sentence that can be written into a resume/CL
   is a provenance-bearing object, which gives accident 4 (claim-verification with unsupported
   assertions) a place to attach.
3. **Conflicts are never silent**: when new extraction differs from the existing value, mark
   ``CONFLICT`` and await user adjudication — never last-write-wins (docs/16 accident 1 lesson:
   tools must not silently overwrite human output).

``work_authorization`` is an **enum**, not a boolean (2026-10-19 note: "must be stated" is
identity-dependent) — FMT rules instantiate per enum value and do not hardcode any single
identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class WorkAuthorization(str, Enum):
    """Work authorization status in Singapore — **identity-dependent**, not "Citizen must be
    stated".

    The FMT resume-header rule (``compliance/rules/format.py``) validates against this enum:
    any legal form passes; only absence is flagged.
    """

    CITIZEN = "sg_citizen"
    PR = "permanent_resident"
    EP = "employment_pass"
    S_PASS = "s_pass"
    NOT_STATED = "not_stated"      # not written in the document -> do not fabricate; visible as needs_human in the review panel

    @property
    def label(self) -> str:
        return {
            "sg_citizen": "Singapore Citizen",
            "permanent_resident": "Permanent Resident",
            "employment_pass": "Employment Pass",
            "s_pass": "S Pass",
            "not_stated": "Not stated in documents",
        }[self.value]

    @classmethod
    def from_text(cls, text: str) -> "WorkAuthorization":
        """Infer the authorization status from the document's original text (**absence means not
        stated**, never default to Citizen).

        "Citizen of Singapore" or simply not written. Defaulting to Citizen = fabricating an
        identity field, which is a class-A4 accident (an assertion without basis).
        """
        t = (text or "").lower()
        if re.search(r"\b(sg|sin)?\s*citizen\s+of\s+singapore\b", t) or re.search(
            r"\bsingapore\s+citizen\b", t
        ):
            return cls.CITIZEN
        if re.search(r"permanent\s+resident", t):
            return cls.PR
        if re.search(r"employment\s+pass", t):
            return cls.EP
        if re.search(r"\bs\s*pass\b", t):
            return cls.S_PASS
        return cls.NOT_STATED


import re  # noqa: E402  (used by WorkAuthorization.from_text; placed below to avoid implying an import-order dependency)


class ClaimStatus(str, Enum):
    """Claim lifecycle — determines how it is handled on re-ingest (docs/19 sec 4.2)."""

    DRAFT = "draft"                    # freshly extracted, not yet seen by a human
    CONFIRMED = "confirmed"            # human-confirmed
    MANUAL_OVERRIDE = "manual_override"  # **human-edited — never overwritten**
    CONFLICT = "conflict"              # conflicts with existing value, awaiting adjudication
    NEEDS_HUMAN = "needs_human"        # extraction confidence too low / no provenance located


class DocFormat(str, Enum):
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    MD = "md"
    JSON_RESUME = "json_resume"        # standard format, ingested directly (no extraction, docs/19 sec 4.4)
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Provenance:
    """The provenance of one fact (field-level traceability — the premise that lets a user judge
    right or wrong within 3 seconds).

    ``locator`` is a string rather than a typed value because each format locates differently:
    - txt/md: ``chars 1180-1216`` (character span over the full text)
    - docx: ``para 12`` (python-docx paragraph index)
    - pdf: ``page 2`` (pypdf page number, 1-based)
    - json_resume: ``/work/0`` (JSON pointer)
    """

    source_file: str
    locator: str
    snippet: str
    extracted_at: str
    extractor: str = "profile-extract-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "locator": self.locator,
            "snippet": self.snippet,
            "extracted_at": self.extracted_at,
            "extractor": self.extractor,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provenance":
        return cls(
            source_file=d.get("source_file", ""),
            locator=d.get("locator", ""),
            snippet=d.get("snippet", ""),
            extracted_at=d.get("extracted_at", ""),
            extractor=d.get("extractor", "profile-extract-v1"),
        )


@dataclass
class Claim:
    """A fact sentence with provenance — **the only unit that may be written into downstream
    materials**.

    ``text`` is the original sentence or its normalized form (never rewritten or polished); the
    downstream ``adapt.fit_to_budget`` only does packaging (delete only, never add — the
    no-new-claims rule), and the ``compliance`` A4 rule attaches its checks here.
    """

    field: str                       # "experiences.0.title" / "identity.work_authorization"
    text: str
    status: ClaimStatus = ClaimStatus.DRAFT
    confidence: float = 0.0          # 0..1; raised by multi-source corroboration
    provenance: list[Provenance] = field(default_factory=list)
    rejected_sources: list[Provenance] = field(default_factory=list)  # sources not adopted during a conflict

    @property
    def pinned(self) -> bool:
        """Human-edited — skipped on re-ingest (merge iron rule 1)."""
        return self.status is ClaimStatus.MANUAL_OVERRIDE

    def normalize(self) -> str:
        """Value normalization (used for the "same field, same value" judgment): whitespace
        collapsing + curly-quote normalization + lowercasing.

        Same idea as ``compliance.rules.accidents._norm_for_match`` (this one does not include
        abbreviation expansion — that is reserved for JD entity-matching rehearsal).
        """
        s = self.text.replace("’", "'").replace("‘", "'")
        return re.sub(r"\s+", " ", s).strip().lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "text": self.text,
            "status": self.status.value,
            "confidence": round(self.confidence, 3),
            "provenance": [p.to_dict() for p in self.provenance],
            "rejected_sources": [p.to_dict() for p in self.rejected_sources],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        return cls(
            field=d.get("field", ""),
            text=d.get("text", ""),
            status=ClaimStatus(d.get("status", "draft")),
            confidence=float(d.get("confidence", 0.0)),
            provenance=[Provenance.from_dict(p) for p in d.get("provenance", [])],
            rejected_sources=[Provenance.from_dict(p) for p in d.get("rejected_sources", [])],
        )


@dataclass
class Experience:
    """One experience (employment / research / project / education all map to this structure).

    ``bullets`` stores **verified sentence units** (a Claim whose field points to
    ``experiences.N.bullets.M``); no bare strings are placed at this layer — avoiding the
    structural hole of "fact sentences with no provenance".
    """

    org: str
    title: str
    start: str = ""            # normalized YYYY-MM or YYYY-MM-DD (normalize_date)
    end: str = ""              # "" / "present" / YYYY-MM
    location: str = ""
    category: str = "employment"   # employment | research | project | education
    bullets: list[Claim] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "org": self.org,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "location": self.location,
            "category": self.category,
            "bullets": [c.to_dict() for c in self.bullets],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Experience":
        return cls(
            org=d.get("org", ""),
            title=d.get("title", ""),
            start=d.get("start", ""),
            end=d.get("end", ""),
            location=d.get("location", ""),
            category=d.get("category", "employment"),
            bullets=[Claim.from_dict(c) for c in d.get("bullets", [])],
        )


@dataclass
class Profile:
    """Personal profile = single source of truth.

    Only ``claims`` is a first-class citizen; the scalar fields of ``identity`` (full_name, etc.)
    are **convenience views**, each backed by a same-named claim — avoiding two-place storage
    drift.
    """

    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    work_authorization: WorkAuthorization = WorkAuthorization.NOT_STATED
    claims: dict[str, Claim] = field(default_factory=dict)   # field -> Claim (flat index)
    experiences: list[Experience] = field(default_factory=list)
    preferences: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "0.1"

    # -- claim index read/write (experiences' bullets also enter the flat index) ----

    def reindex(self) -> None:
        """Rebuild the flat claim index (called after load; experiences' bullets sync into the index)."""
        self.claims = {}
        for key in ("identity.full_name", "identity.email", "identity.phone",
                    "identity.location", "identity.work_authorization"):
            c = self.claims.get(key)
            if c:
                self.claims[key] = c
        for i, exp in enumerate(self.experiences):
            for k, (field_name, val) in enumerate({
                "org": exp.org, "title": exp.title, "start": exp.start,
                "end": exp.end, "location": exp.location,
            }.items()):
                if not val:
                    continue
                key = f"experiences.{i}.{field_name}"
                if key not in self.claims:
                    self.claims[key] = Claim(field=key, text=val,
                                             status=ClaimStatus.CONFIRMED)
            for j, bullet in enumerate(exp.bullets):
                self.claims[f"experiences.{i}.bullets.{j}"] = bullet

    def claim(self, field_name: str) -> Optional[Claim]:
        return self.claims.get(field_name)

    def confirmed_claims(self) -> list[Claim]:
        """Confirmed + human-edited claims — **the only set allowed into downstream materials**.

        DRAFT/CONFLICT/NEEDS_HUMAN are never included (docs/19 sec 4.3: low confidence is
        surfaced for human adjudication).
        """
        return [c for c in self.claims.values()
                if c.status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE)]

    def pinned_fields(self) -> list[str]:
        return sorted(k for k, c in self.claims.items() if c.pinned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "full_name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "location": self.location,
            "work_authorization": self.work_authorization.value,
            "claims": {k: c.to_dict() for k, c in sorted(self.claims.items())},
            "experiences": [e.to_dict() for e in self.experiences],
            "preferences": self.preferences,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        p = cls(
            full_name=d.get("full_name", ""),
            email=d.get("email", ""),
            phone=d.get("phone", ""),
            location=d.get("location", ""),
            work_authorization=WorkAuthorization(d.get("work_authorization",
                                                       "not_stated")),
            preferences=d.get("preferences", {}) or {},
            schema_version=d.get("schema_version", "0.1"),
            experiences=[Experience.from_dict(e) for e in d.get("experiences", [])],
        )
        p.claims = {k: Claim.from_dict(v) for k, v in (d.get("claims") or {}).items()}
        return p

    # -- convenience view sync (call after scalar fields change) ----

    def sync_identity_from_claims(self) -> None:
        """Backfill the identity scalar view from the claim index (the direction of single truth is
        claim -> view)."""
        for key, attr in (("identity.full_name", "full_name"),
                          ("identity.email", "email"),
                          ("identity.phone", "phone"),
                          ("identity.location", "location")):
            c = self.claims.get(key)
            if c and c.status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
                setattr(self, attr, c.text)
        c = self.claims.get("identity.work_authorization")
        if c and c.status in (ClaimStatus.CONFIRMED, ClaimStatus.MANUAL_OVERRIDE):
            try:
                self.work_authorization = WorkAuthorization(c.text)
            except ValueError:
                pass


def save_json(obj: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path
