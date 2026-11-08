"""Employer classification: watchlists first, LLM region fallback (PLAN W4, D-93)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.models import PROGRAM_TYPES, Kind, OpportunityType, kind_to_type
from src.ranking.weights import Weights

LEGAL_FORMS = frozenset(
    [
        "llc",
        "llp",
        "inc",
        "ltd",
        "limited",
        "plc",
        "gmbh",
        "ag",
        "sa",
        "bv",
        "jsc",
        "ао",
        "тоо",
        "ооо",
    ]
)
_EDGE_WORDS = frozenset({"the"})
_PUNCT = re.compile(r"[^\w.]+", re.UNICODE)
_DOT = re.compile(r"(?<!\w)\.|\.(?!\w)")


@dataclass(frozen=True)
class Classification:
    category: str  # intl_top_tier|intl_other|kz_employer|kz_startup|kz_program|unknown
    source: str  # watchlist|llm|hint|none


def normalize_company(name: str | None) -> str:
    """Normalize a company name for exact matching (no fuzzy matching, D-93)."""
    s = unicodedata.normalize("NFKC", name or "").casefold().replace("ё", "е")
    s = s.replace("_", " ")
    s = _PUNCT.sub(" ", s)
    s = _DOT.sub(" ", s)
    tokens = [t for t in s.split() if t not in LEGAL_FORMS]
    while tokens and tokens[0] in _EDGE_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1] in _EDGE_WORDS:
        tokens.pop()
    return " ".join(tokens)


def _kz(startup: bool, opportunity_type: str | None, kind: Kind | str | None) -> str:
    """One KZ category, decided once (D-248): programs never stack with startup/employer."""
    eff = opportunity_type
    if not eff and kind:
        eff = kind_to_type(kind)
    try:
        eff = OpportunityType(eff) if eff else None
    except ValueError:
        eff = None
    if eff in PROGRAM_TYPES:
        return "kz_program"
    if eff == OpportunityType.STARTUP_ROLE:
        startup = True
    return "kz_startup" if startup else "kz_employer"


def classify_employer(
    company: str | None,
    kind: Kind | str | None,
    region: str,
    confidence: float,
    is_startup: bool | None,
    weights: Weights,
    hint: str | None = None,
    opportunity_type: str | None = None,
) -> Classification:
    norm = normalize_company(company)
    if norm:
        for e in weights.intl_top_tier:
            if norm in {normalize_company(n) for n in (e.name, *e.aliases)}:
                return Classification("intl_top_tier", "watchlist")
        for k in weights.kz_employers:
            if norm in {normalize_company(n) for n in (k.name, *k.aliases)}:
                return Classification(_kz(k.startup, opportunity_type, kind), "watchlist")
    if confidence >= weights.region_min_confidence:
        if region == "kz":
            startup = is_startup is True or str(kind) == Kind.STARTUP.value
            return Classification(_kz(startup, opportunity_type, kind), "llm")
        if region == "intl":
            return Classification("intl_other", "llm")
    if hint == "kz":  # D-194: only after the watchlist, and only for unknown/low-confidence
        startup = is_startup is True or str(kind) == Kind.STARTUP.value
        return Classification(_kz(startup, opportunity_type, kind), "hint")
    return Classification("unknown", "none")
