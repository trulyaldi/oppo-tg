"""Shared data models (SPEC/PLAN 1.4)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class Kind(StrEnum):
    JOB = "job"
    INTERNSHIP = "internship"
    HACKATHON = "hackathon"
    COMPETITION = "competition"
    FELLOWSHIP = "fellowship"
    RESEARCH = "research"
    STARTUP = "startup"
    OTHER = "other"


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
    deadline: date | None = None
    kind: Kind = Kind.OTHER
    raw_tags: tuple[str, ...] = ()

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
    deadline: date | None = None
    red_flags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    employer_region: Literal["kz", "intl", "unknown"] = "unknown"
    region_confidence: float = Field(0.0, ge=0, le=1)
    employer_is_startup: bool | None = None
    visa_barrier: bool | None = None
