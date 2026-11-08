"""Deterministic market summary of requested skills and qualifications (D-259, KF-31).

No LLM is called. Postings first seen in the last `market_days` that scored at least
`market_min_score` (base score) or carry an active like are split by employer bucket (KZ or
international; unknown is left out). Each posting counts each `fields.*` / `qualifications.*` cue
group once; the top groups per bucket come back with counts and shares.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.clock import Clock
from src.config import CvReviewConfig
from src.cues import Cues, match_any
from src.ranking.weights import bucket


@dataclass(frozen=True)
class MarketBucket:
    """`groups` are `(group_label, count, share)`, best first; share is count / n_postings."""

    n_postings: int = 0
    groups: list[tuple[str, int, float]] = field(default_factory=list)
    enough: bool = False


@dataclass(frozen=True)
class MarketSummary:
    kz: MarketBucket = field(default_factory=MarketBucket)
    intl: MarketBucket = field(default_factory=MarketBucket)


def _label(name: str) -> str:
    return name.split(".", 1)[-1]


def _build(
    texts: list[str], group_names: list[str], cues: Cues, cfg: CvReviewConfig
) -> MarketBucket:
    n = len(texts)
    counts: dict[str, int] = {}
    for text in texts:
        for name in group_names:
            if match_any(cues[name], text):
                counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.top_n]
    groups = [(_label(name), c, round(c / n, 4)) for name, c in ranked]
    return MarketBucket(n_postings=n, groups=groups, enough=n >= cfg.min_postings)


def market_summary(
    conn: sqlite3.Connection, cues: Cues, cfg: CvReviewConfig, clock: Clock
) -> MarketSummary:
    cutoff = clock.now() - timedelta(days=cfg.market_days)
    rows = conn.execute(
        "SELECT p.title, p.description, p.first_seen_at, s.employer_category,"
        " s.base_score, EXISTS(SELECT 1 FROM reactions r WHERE r.posting_id=p.id"
        " AND r.action='like' AND r.active=1) AS liked"
        " FROM postings p JOIN scores s ON s.posting_id=p.id"
    ).fetchall()
    texts: dict[str, list[str]] = {"kz": [], "intl": []}
    for row in rows:
        side = bucket(row["employer_category"])
        if side is None:
            continue
        if datetime.fromisoformat(row["first_seen_at"]) < cutoff:
            continue
        if not (row["liked"] or row["base_score"] >= cfg.market_min_score):
            continue
        texts[side].append(f"{row['title']}\n{row['description']}")
    names = cues.names("fields") + cues.names("qualifications")
    return MarketSummary(
        kz=_build(texts["kz"], names, cues, cfg),
        intl=_build(texts["intl"], names, cues, cfg),
    )
