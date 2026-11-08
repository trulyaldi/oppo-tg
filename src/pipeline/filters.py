"""Hard filters (spam, unpaid, senior-only, stale) and the title relevance prefilter."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.config import AppConfig
from src.cues import Cues, load_cues, match_any, which
from src.models import Kind, Posting

LOCAL_TZ = ZoneInfo("Asia/Almaty")
DEFAULT_MAX_AGE_DAYS = 60
_JOB_KINDS = (Kind.JOB, Kind.INTERNSHIP)
_NO_UNPAID_CHECK = (Kind.HACKATHON, Kind.COMPETITION, Kind.FELLOWSHIP)
# Sources whose pages the user curates by hand: their items skip the title relevance check.
_RELEVANCE_EXEMPT_SOURCES = ("pagewatch",)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DROP_REASON = {"drop:seniority": "senior_only", "drop:negative": "negative_title"}


def load_filter_cues(cfg: AppConfig) -> Cues:
    """The cue file named by [filters] cues_file (relative to the repo root); a bad file raises."""
    raw = cfg.filters.get("cues_file")
    if raw is None:
        return load_cues()
    path = Path(str(raw))
    return load_cues(path if path.is_absolute() else _REPO_ROOT / path)


def hard_blocker(p: Posting, cfg: AppConfig, cues: Cues | None = None) -> str | None:
    """First work-authorization phrase of the cue file found in title or description (D-195)."""
    cues = cues or load_filter_cues(cfg)
    found = which(cues["hard_blockers"], f"{p.title}\n{p.description}")
    return found[0] if found else None


def hard_blocker_action(cfg: AppConfig) -> str:
    action = str(cfg.filters.get("hard_blocker_action", "lower")).strip().lower()
    return action if action in ("lower", "filter") else "lower"


def hard_blocker_penalty(cfg: AppConfig) -> float:
    try:
        return float(cfg.filters.get("hard_blocker_penalty", 2.0))
    except (TypeError, ValueError):
        return 2.0


def hard_filter(p: Posting, cfg: AppConfig, today: date, cues: Cues | None = None) -> str | None:
    """Return a filter reason, or None if the posting passes."""
    f: dict[str, Any] = cfg.filters
    cues = cues or load_filter_cues(cfg)
    haystack = f"{p.title}\n{p.description}"
    if match_any(cues["spam"], haystack):
        return "spam"
    if p.kind not in _NO_UNPAID_CHECK and match_any(cues["unpaid"], haystack):
        return "unpaid"
    if p.kind in _JOB_KINDS:
        reason = _DROP_REASON.get(cues.title_decision(p.title))
        if reason is not None:
            return reason
    if p.deadline is not None and p.deadline < today:
        return "stale"
    if hard_blocker_action(cfg) == "filter" and hard_blocker(p, cfg, cues):
        return "hard_blocker"
    max_age = int(f.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    if p.posted_at is not None:
        posted = (
            p.posted_at.astimezone(LOCAL_TZ).date() if p.posted_at.tzinfo else p.posted_at.date()
        )
        if (today - posted) > timedelta(days=max_age):
            return "stale"
    return None


def relevance_ok(p: Posting, cfg: AppConfig, cues: Cues | None = None) -> bool:
    """JOB/INTERNSHIP titles must match a positive cue; other kinds always pass."""
    if p.kind not in _JOB_KINDS or p.source in _RELEVANCE_EXEMPT_SOURCES:
        return True
    cues = cues or load_filter_cues(cfg)
    return cues.is_positive(p.title)


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
    cues = load_filter_cues(cfg)
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT * FROM postings WHERE status='new'").fetchall():
        p = _row_to_posting(row)
        reason = hard_filter(p, cfg, today, cues)
        if reason is None and not relevance_ok(p, cfg, cues):
            reason = "irrelevant"
        if reason is None and hard_blocker_action(cfg) == "lower":
            phrase = hard_blocker(p, cfg, cues)
            if phrase != row["hard_blocker"]:
                conn.execute("UPDATE postings SET hard_blocker=? WHERE id=?", (phrase, row["id"]))
                conn.commit()
        if reason is not None:
            db.set_status(conn, row["id"], "filtered", reason)
            counts[reason] = counts.get(reason, 0) + 1
    return counts
