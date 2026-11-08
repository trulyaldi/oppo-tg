"""Post-triage step (D-246, D-262): unpaid by type, scam filter, repost dedupe.

Runs over `triaged` rows (and `new` rows when triage is off), before scoring.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta

from src import db
from src.clock import Clock
from src.cues import Cues, match_any
from src.models import Kind, OpportunityType, kind_to_type
from src.pipeline.dedupe import _norm

REPOST_WINDOW_DAYS = 30
_UNPAID_TYPES = (OpportunityType.JOB.value, OpportunityType.INTERNSHIP.value)
_SCAM_FILTERED = ("upfront_payment", "other")
MANUAL_SOURCE = "manual"


def effective_type(opportunity_type: str | None, kind: str) -> str:
    """The stored type, or the type mapped from the source kind when the column is empty."""
    return opportunity_type or kind_to_type(Kind(kind)).value


def is_unpaid(cues: Cues, title: str, description: str, opp_type: str) -> bool:
    """Unpaid cues on title plus description, only for job and internship types (D-246)."""
    return opp_type in _UNPAID_TYPES and match_any(cues["unpaid"], f"{title}\n{description}")


def repost_key(company: str, title: str, contact_key: str | None) -> str | None:
    """D-262: set only with a non-empty title and a non-empty company or contact."""
    company, title = _norm(company or ""), _norm(title or "")
    if not title or not (company or contact_key):
        return None
    raw = f"{company}|{title}|{contact_key or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _find_original(conn: sqlite3.Connection, row: sqlite3.Row, key: str, since: str) -> str | None:
    cands = conn.execute(
        "SELECT id, target, source FROM postings WHERE repost_key=? AND id != ?"
        " AND status != 'duplicate' AND first_seen_at >= ?"
        " AND (first_seen_at < ? OR (first_seen_at = ? AND rowid < ?))"
        " ORDER BY first_seen_at, rowid LIMIT 20",
        (key, row["id"], since, row["first_seen_at"], row["first_seen_at"], row["rowid"]),
    ).fetchall()
    for c in cands:
        if (
            c["target"] != row["target"]
            or row["source"] == MANUAL_SOURCE
            or c["source"] == MANUAL_SOURCE
        ):
            return str(c["id"])
    return None


def apply_post_triage(
    conn: sqlite3.Connection,
    cues: Cues,
    clock: Clock,
    include_new: bool = False,
    only_id: str | None = None,
) -> dict[str, int]:
    """Filter unpaid and scam rows, mark reposts; returns counts by reason.

    `include_new` also covers `new` rows (triage disabled); with triage on those still await it.
    """
    statuses = ("triaged", "new") if include_new else ("triaged",)
    marks = ",".join("?" for _ in statuses)
    since = (clock.now() - timedelta(days=REPOST_WINDOW_DAYS)).isoformat()
    only = " AND id=?" if only_id is not None else ""
    rows = conn.execute(
        f"SELECT rowid AS rowid, * FROM postings WHERE status IN ({marks}){only}"
        " ORDER BY first_seen_at, rowid",
        (*statuses, *([only_id] if only_id is not None else [])),
    ).fetchall()
    counts: dict[str, int] = {}

    def drop(row: sqlite3.Row, status: str, reason: str) -> None:
        db.set_status(conn, row["id"], status, reason)
        counts[reason] = counts.get(reason, 0) + 1

    for row in rows:
        opp = effective_type(row["opportunity_type"], row["kind"])
        if is_unpaid(cues, row["title"], row["description"], opp):
            drop(row, "filtered", "unpaid")
        elif row["scam"] in _SCAM_FILTERED and row["source"] != MANUAL_SOURCE:
            drop(row, "filtered", "scam")
        else:
            key = repost_key(
                row["company"] or row["ex_company"] or "",
                row["title"] or row["ex_title"] or "",
                row["contact_key"],
            )
            if key is None:
                continue
            with conn:
                conn.execute("UPDATE postings SET repost_key=? WHERE id=?", (key, row["id"]))
            if _find_original(conn, row, key, since) is not None:
                drop(row, "duplicate", "repost")
    return counts
