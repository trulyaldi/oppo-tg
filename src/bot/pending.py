"""Generic pending-input state with expiry, stored in `state` (C4, D-102)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from src import db

KEY = "pending_input"


@dataclass(frozen=True)
class Pending:
    kind: str
    posting_id: str | None
    expires_at: datetime


def set_pending(
    conn: sqlite3.Connection, kind: str, posting_id: str | None, now: datetime, minutes: int
) -> None:
    payload = {
        "kind": kind,
        "posting_id": posting_id,
        "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
    }
    db.set_state(conn, KEY, json.dumps(payload))


def clear_pending(conn: sqlite3.Connection) -> bool:
    """Remove any pending state; True if there was one."""
    had = db.get_state(conn, KEY) is not None
    conn.execute("DELETE FROM state WHERE key=?", (KEY,))
    conn.commit()
    return had


def peek_pending(conn: sqlite3.Connection, now: datetime) -> Pending | None:
    """Current unexpired pending input without clearing it."""
    raw = db.get_state(conn, KEY)
    if raw is None:
        return None
    try:
        d = json.loads(raw)
        p = Pending(d["kind"], d.get("posting_id"), datetime.fromisoformat(d["expires_at"]))
    except (ValueError, KeyError, TypeError):
        return None
    return p if now < p.expires_at else None


def take_pending(conn: sqlite3.Connection, now: datetime) -> Pending | None:
    """Return and clear the pending input; None if absent or expired (cleared either way)."""
    p = peek_pending(conn, now)
    clear_pending(conn)
    return p
