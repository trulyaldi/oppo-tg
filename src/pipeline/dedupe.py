"""Cross-source duplicate detection by normalized company + title."""

from __future__ import annotations

import re
import sqlite3
import unicodedata

from src import db
from src.models import Posting

_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return _NON_ALNUM.sub(" ", text).strip()


def fingerprint(p: Posting) -> str:
    """Normalized company + title. Without a company, the source is included so that
    unrelated postings from different feeds do not collapse into each other."""
    company = _norm(p.company) or f"@{p.source}"
    return f"{company}|{_norm(p.title)}"


def mark_duplicates(conn: sqlite3.Connection) -> int:
    """Mark 'new' postings whose fingerprint was already seen earlier as duplicate."""
    rows = conn.execute(
        "SELECT id, fingerprint, status FROM postings ORDER BY first_seen_at, rowid"
    ).fetchall()
    seen: set[str] = set()
    count = 0
    for row in rows:
        fp = row["fingerprint"]
        if fp in seen:
            if row["status"] == "new":
                db.set_status(conn, row["id"], "duplicate", "duplicate")
                count += 1
        else:
            seen.add(fp)
    return count
