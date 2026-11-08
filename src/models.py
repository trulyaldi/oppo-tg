"""Shared data models (SPEC/PLAN 1.4)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from src.scoring.reason import FLAG_MAX, clip_reason, clip_text


class Kind(StrEnum):
    JOB = "job"
    INTERNSHIP = "internship"
    HACKATHON = "hackathon"
    COMPETITION = "competition"
    FELLOWSHIP = "fellowship"
    RESEARCH = "research"
    STARTUP = "startup"
    OTHER = "other"


class OpportunityType(StrEnum):
    JOB = "job"
    INTERNSHIP = "internship"
    RESEARCH = "research"
    FELLOWSHIP = "fellowship"
    SCHOLARSHIP = "scholarship"
    GRANT = "grant"
    HACKATHON = "hackathon"
    COMPETITION = "competition"
    ACCELERATOR = "accelerator"
    SUMMER_SCHOOL = "summer_school"
    STARTUP_ROLE = "startup_role"
    OTHER = "other"


PROGRAM_TYPES = frozenset(
    {
        OpportunityType.GRANT,
        OpportunityType.SCHOLARSHIP,
        OpportunityType.FELLOWSHIP,
        OpportunityType.HACKATHON,
        OpportunityType.COMPETITION,
        OpportunityType.ACCELERATOR,
        OpportunityType.SUMMER_SCHOOL,
    }
)

_KIND_TO_TYPE = {
    Kind.JOB: OpportunityType.JOB,
    Kind.INTERNSHIP: OpportunityType.INTERNSHIP,
    Kind.HACKATHON: OpportunityType.HACKATHON,
    Kind.COMPETITION: OpportunityType.COMPETITION,
    Kind.FELLOWSHIP: OpportunityType.FELLOWSHIP,
    Kind.RESEARCH: OpportunityType.RESEARCH,
    Kind.STARTUP: OpportunityType.STARTUP_ROLE,
    Kind.OTHER: OpportunityType.OTHER,
}


def kind_to_type(kind: Kind | str) -> OpportunityType:
    """Map the source's coarse kind to an opportunity type (D-247); unknown gives `other`."""
    try:
        return _KIND_TO_TYPE[Kind(kind)]
    except ValueError:
        return OpportunityType.OTHER


MAX_DESCRIPTION_CHARS = 8000


@dataclass(frozen=True)
class Posting:
    source: str
    external_id: str
    title: str
    company: str
    url: str
    location: str = ""
    remote: bool | None = None
    description: str = ""
    posted_at: datetime | None = None
    published_at: datetime | None = None  # API publish time; extra data only (D-185)
    deadline: date | None = None
    kind: Kind = Kind.OTHER
    raw_tags: tuple[str, ...] = ()
    opportunity_type: str | None = None  # config-asserted type (type_source='config', KF-25)

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"posting url must be http(s): {self.url!r}")
        if len(self.description) > MAX_DESCRIPTION_CHARS:
            object.__setattr__(self, "description", self.description[:MAX_DESCRIPTION_CHARS])

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.source}:{self.external_id}".encode()).hexdigest()[:16]


class ScoreResult(BaseModel):
    score: float = Field(ge=0, le=10)
    reason: str
    type: OpportunityType = OpportunityType.OTHER
    deadline: date | None = None
    red_flags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    employer_region: Literal["kz", "intl", "unknown"] = "unknown"
    region_confidence: float = Field(0.0, ge=0, le=1)
    employer_is_startup: bool | None = None
    visa_barrier: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_invalid_type(cls, data: Any) -> Any:
        """A missing or invalid type is not a parse failure (D-251). It stays `other` and is
        left out of model_fields_set, so the scorer does not override the triage type."""
        if isinstance(data, dict) and "type" in data:
            try:
                OpportunityType(data["type"])
            except ValueError:
                data = {k: v for k, v in data.items() if k != "type"}
        return data

    @field_validator("reason")
    @classmethod
    def _clip_reason(cls, v: str) -> str:
        return clip_reason(v)

    @field_validator("red_flags")
    @classmethod
    def _clip_flags(cls, v: list[str]) -> list[str]:
        return [clip_text(f, FLAG_MAX) for f in v[:3]]
