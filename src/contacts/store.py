"""Contact storage: name, role, company, source URL, note, expiry only (N1, D-103).

Emails and phones are redacted before storage. Nothing here logs contact fields.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from src.contacts.classify import classify_role
from src.contacts.sanitize import NAME_MAX, NOTE_MAX, ROLE_MAX, clean_field

log = logging.getLogger(__name__)

SOURCE_KINDS = ("posting_text", "hn", "team_page", "organizer_page", "pasted")


@dataclass(frozen=True)
class RawContact:
    """Shared return type of every contact parser."""

    name: str
    role: str = ""
    note: str = ""


def _safe_url(url: str) -> str:
    url = (url or "").strip()
    if len(url) > 500:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return url


def _deadline_for(conn: sqlite3.Connection, posting_id: str) -> date | None:
    row = conn.execute(
        "SELECT p.deadline AS pd, s.deadline AS sd FROM postings p"
        " LEFT JOIN scores s ON s.posting_id = p.id WHERE p.id = ?",
        (posting_id,),
    ).fetchone()
    if row is None:
        return None
    for val in (row["pd"], row["sd"]):
        if val:
            try:
                return date.fromisoformat(str(val)[:10])
            except ValueError:
                continue
    return None


def compute_expiry(now: datetime, ttl_days: int, deadline: date | None) -> datetime:
    expiry = now + timedelta(days=ttl_days)
    if deadline is not None:
        cut = datetime.combine(deadline + timedelta(days=1), time(0), tzinfo=now.tzinfo)
        expiry = min(expiry, cut)
    return expiry


def add_contact(
    conn: sqlite3.Connection,
    posting_id: str,
    raw: RawContact,
    *,
    source_kind: str,
    source_url: str = "",
    company: str = "",
    now: datetime,
    ttl_days: int = 60,
    max_per_posting: int = 10,
) -> int | None:
    """Store one contact; returns its id, or None if empty, duplicate or over the cap."""
    if source_kind not in SOURCE_KINDS:
        raise ValueError("unknown source_kind")
    name = clean_field(raw.name, NAME_MAX)
    if not name:
        return None
    role = clean_field(raw.role, ROLE_MAX)
    note = clean_field(raw.note, NOTE_MAX)
    url = _safe_url(source_url)
    n = conn.execute("SELECT COUNT(*) FROM contacts WHERE posting_id=?", (posting_id,)).fetchone()[
        0
    ]
    if n >= max_per_posting:
        return None
    ctype, conf = classify_role(role)
    expires = compute_expiry(now, ttl_days, _deadline_for(conn, posting_id))
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO contacts (posting_id, name, role, company, contact_type,"
            " type_confidence, source_kind, source_url, note, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                posting_id,
                name,
                role,
                clean_field(company, ROLE_MAX),
                ctype,
                conf,
                source_kind,
                url,
                note,
                now.isoformat(),
                expires.isoformat(),
            ),
        )
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def contacts_for(conn: sqlite3.Connection, posting_id: str, now: datetime) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM contacts WHERE posting_id=? ORDER BY type_confidence DESC, id",
        (posting_id,),
    ).fetchall()
    return [r for r in rows if datetime.fromisoformat(r["expires_at"]) > now]


def get_contact(conn: sqlite3.Connection, contact_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()


def prune_contacts(conn: sqlite3.Connection, now: datetime) -> int:
    """Delete expired contacts (drafts cascade). Compares parsed times, not strings."""
    rows = conn.execute("SELECT id, expires_at FROM contacts").fetchall()
    dead = [r["id"] for r in rows if datetime.fromisoformat(r["expires_at"]) <= now]
    with conn:
        for cid in dead:
            conn.execute("DELETE FROM contacts WHERE id=?", (cid,))
    if dead:
        log.info("pruned %d expired contacts", len(dead))
    return len(dead)
