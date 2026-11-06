"""LLM CV section parse with deterministic grounding (C2, D-98, D-107)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from src.config import OutreachConfig
from src.cv.extract import norm
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.llm.untrusted import wrap_untrusted

PROMPT_PATH = Path(__file__).parent / "prompts" / "parse_cv.md"
MAX_TOKENS = 2000


def prompt_version() -> str:
    return hashlib.sha256(PROMPT_PATH.read_bytes()).hexdigest()[:12]


class Education(BaseModel):
    institution: str = ""
    degree: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    details: list[str] = Field(default_factory=list)


class Experience(BaseModel):
    organization: str = ""
    role: str = ""
    start: str = ""
    end: str = ""
    bullets: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)


class CvSections(BaseModel):
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    other: list[str] = Field(default_factory=list)


@dataclass
class ParseResult:
    sections: CvSections | None = None
    error: str | None = None  # budget | api | invalid | no sections
    dropped: int = 0
    calls: int = 0


class _Grounder:
    def __init__(self, cv_text: str):
        self.cv = norm(cv_text)
        self.dropped = 0

    def ok(self, s: str) -> bool:
        n = norm(s)
        return bool(n) and n in self.cv

    def quote_ok(self, s: str) -> bool:  # V1: non-empty, >= 8 chars, substring
        n = norm(s)
        return len(n) >= 8 and n in self.cv

    def strs(self, items: list[str], quote: bool = False) -> list[str]:
        keep = []
        for s in items:
            if self.quote_ok(s) if quote else self.ok(s):
                keep.append(s)
            else:
                self.dropped += 1
        return keep

    def required(self, s: str) -> bool:
        if self.ok(s):
            return True
        self.dropped += 1
        return False


def ground_sections(sec: CvSections, cv_text: str) -> tuple[CvSections, int]:
    g = _Grounder(cv_text)
    out = CvSections()
    for e in sec.education:
        if not g.required(e.institution):
            continue
        degree = e.degree
        if degree and not g.ok(degree):
            g.dropped += 1
            degree = ""
        out.education.append(
            e.model_copy(update={"degree": degree, "details": g.strs(e.details, quote=True)})
        )
    for x in sec.experience:
        if not (g.required(x.organization) and g.required(x.role)):
            continue
        out.experience.append(x.model_copy(update={"bullets": g.strs(x.bullets, quote=True)}))
    for p in sec.projects:
        if not g.required(p.name):
            continue
        out.projects.append(
            p.model_copy(update={"bullets": g.strs(p.bullets, quote=True), "tech": g.strs(p.tech)})
        )
    out.skills = g.strs(sec.skills)
    out.other = g.strs(sec.other, quote=True)
    return out, g.dropped


async def parse_cv_sections(
    client: Any, text: str, cfg: OutreachConfig, budget: LlmBudget
) -> ParseResult:
    system = [
        {
            "type": "text",
            "text": PROMPT_PATH.read_text(encoding="utf-8"),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    res = await call_json(
        client,
        model=cfg.cheap_model,
        system_blocks=system,
        user_text=wrap_untrusted("cv", text),
        schema=CvSections,
        max_tokens=MAX_TOKENS,
        budget=budget,
        purpose="parse_cv",
    )
    if res.error_kind:
        return ParseResult(error=res.error_kind, calls=res.calls)
    sections, dropped = ground_sections(res.value, text)
    if not (sections.education or sections.experience):
        return ParseResult(error="no sections", dropped=dropped, calls=res.calls)
    return ParseResult(sections=sections, dropped=dropped, calls=res.calls)
