"""Hard filters (spam, unpaid, senior-only, stale) and the title relevance prefilter."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.config import AppConfig
from src.models import Kind, Posting
from src.text import INTERN_RE

LOCAL_TZ = ZoneInfo("Asia/Almaty")
DEFAULT_MAX_AGE_DAYS = 60
_JOB_KINDS = (Kind.JOB, Kind.INTERNSHIP)
_NO_UNPAID_CHECK = (Kind.HACKATHON, Kind.COMPETITION)
_EARN_SPAM = re.compile(
    r"\bearn\s+(?:up to\s+)?[$€£]\s?\d[\d,]*\s*(?:/|per\s+)\s*(?:day|week|hour)", re.I
)


def _kw_match(keyword: str, text: str, prefix: bool) -> bool:
    if keyword.casefold() == "intern":
        return INTERN_RE.search(text) is not None
    pat = r"\b" + re.escape(keyword.casefold()) + (r"\w*" if prefix else r"\b")
    return re.search(pat, text.casefold()) is not None


def _any_kw(keywords: list[str], text: str) -> bool:
    # Short keywords (ml, ai) must match whole words; longer ones match as prefixes
    # so that "intern" covers "internship" and "engineer" covers "engineering".
    return any(_kw_match(k, text, prefix=len(k) > 3) for k in keywords)


def hard_filter(p: Posting, cfg: AppConfig, today: date) -> str | None:
    """Return a filter reason, or None if the posting passes."""
    f: dict[str, Any] = cfg.filters
    haystack = f"{p.title}\n{p.description}"
    if _any_kw(list(f.get("spam_keywords", [])), haystack) or _EARN_SPAM.search(haystack):
        return "spam"
    if p.kind not in _NO_UNPAID_CHECK and _any_kw(list(f.get("unpaid_keywords", [])), haystack):
        return "unpaid"
    if (
        p.kind in _JOB_KINDS
        and not INTERN_RE.search(p.title)
        and _any_kw(list(f.get("senior_only_keywords", [])), p.title)
    ):
        return "senior_only"
    if p.deadline is not None and p.deadline < today:
        return "stale"
    max_age = int(f.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    if p.posted_at is not None:
        posted = (
            p.posted_at.astimezone(LOCAL_TZ).date() if p.posted_at.tzinfo else p.posted_at.date()
        )
        if (today - posted) > timedelta(days=max_age):
            return "stale"
    return None


def relevance_ok(p: Posting, cfg: AppConfig) -> bool:
    """JOB/INTERNSHIP titles must match a positive keyword; other kinds always pass."""
    if p.kind not in _JOB_KINDS:
        return True
    return _any_kw(list(cfg.filters.get("positive_title_keywords", [])), p.title)


def _row_to_posting(row: sqlite3.Row) -> Posting:
    return Posting(
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        location=row["location"],
        remote=None if row["remote"] is None else bool(row["remote"]),
        description=row["description"],
        posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
        deadline=date.fromisoformat(row["deadline"]) if row["deadline"] else None,
        kind=Kind(row["kind"]),
        raw_tags=tuple(json.loads(row["raw_tags"])),
    )


def apply_filters(conn: sqlite3.Connection, cfg: AppConfig, clock: Clock) -> dict[str, int]:
    """Set status 'filtered' + reason on failing 'new' postings; returns counts by reason."""
    today = clock.now().astimezone(LOCAL_TZ).date()
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT * FROM postings WHERE status='new'").fetchall():
        p = _row_to_posting(row)
        reason = hard_filter(p, cfg, today)
        if reason is None and not relevance_ok(p, cfg):
            reason = "irrelevant"
        if reason is not None:
            db.set_status(conn, row["id"], "filtered", reason)
            counts[reason] = counts.get(reason, 0) + 1
    return counts
