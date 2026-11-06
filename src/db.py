"""SQLite connection, schema migration and repository functions (PLAN 1.7, D-10)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from src.models import Posting, ScoreResult

SCHEMA_VERSION = 1
MAX_SCORE_ATTEMPTS = 3
REACTION_ACTIONS = ("like", "dislike", "save", "applied")
_EXCLUSIVE = {"like": "dislike", "dislike": "like"}

SCHEMA_1 = """
CREATE TABLE postings (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
  title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
  location TEXT NOT NULL DEFAULT '', remote INTEGER, description TEXT NOT NULL DEFAULT '',
  posted_at TEXT, deadline TEXT, kind TEXT NOT NULL DEFAULT 'other',
  raw_tags TEXT NOT NULL DEFAULT '[]',
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  filter_reason TEXT, score_attempts INTEGER NOT NULL DEFAULT 0,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE INDEX idx_postings_status ON postings(status);
CREATE INDEX idx_postings_fp ON postings(fingerprint);
CREATE TABLE scores (
  posting_id TEXT PRIMARY KEY REFERENCES postings(id), score REAL NOT NULL,
  reason TEXT NOT NULL, deadline TEXT, red_flags TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]', model TEXT NOT NULL, prompt_version TEXT NOT NULL,
  scored_at TEXT NOT NULL
);
CREATE TABLE deliveries (
  posting_id TEXT PRIMARY KEY REFERENCES postings(id),
  kind TEXT NOT NULL, chat_id TEXT NOT NULL, message_id INTEGER, sent_at TEXT NOT NULL
);
CREATE TABLE reactions (
  posting_id TEXT NOT NULL REFERENCES postings(id), action TEXT NOT NULL,
  active INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (posting_id, action)
);
CREATE TABLE reaction_log (
  id INTEGER PRIMARY KEY, posting_id TEXT NOT NULL, action TEXT NOT NULL,
  active INTEGER NOT NULL, at TEXT NOT NULL
);
CREATE TABLE source_runs (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  ok INTEGER, n_items INTEGER, n_new INTEGER, error TEXT
);
CREATE TABLE http_cache (
  url TEXT PRIMARY KEY, status INTEGER, etag TEXT, last_modified TEXT, body TEXT,
  fetched_at TEXT NOT NULL
);
CREATE TABLE robots_cache (
  host TEXT PRIMARY KEY, body TEXT, allowed_all INTEGER, fetched_at TEXT NOT NULL
);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE page_snapshots (
  url TEXT PRIMARY KEY, content_hash TEXT NOT NULL, seen_at TEXT NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and Row factory."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations; idempotent."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        with conn:
            for stmt in SCHEMA_1.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


# --- postings -------------------------------------------------------------


def upsert_posting(conn: sqlite3.Connection, p: Posting, fingerprint: str, now: datetime) -> bool:
    """Insert a posting (True) or only bump last_seen_at if it exists (False)."""
    ts = _iso(now)
    cur = conn.execute(
        "INSERT OR IGNORE INTO postings (id, source, external_id, title, company, url, location,"
        " remote, description, posted_at, deadline, kind, raw_tags, fingerprint,"
        " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            p.id,
            p.source,
            p.external_id,
            p.title,
            p.company,
            p.url,
            p.location,
            None if p.remote is None else int(p.remote),
            p.description,
            _iso(p.posted_at) if p.posted_at else None,
            p.deadline.isoformat() if p.deadline else None,
            p.kind.value,
            json.dumps(list(p.raw_tags)),
            fingerprint,
            ts,
            ts,
        ),
    )
    if cur.rowcount == 1:
        conn.commit()
        return True
    conn.execute("UPDATE postings SET last_seen_at=? WHERE id=?", (ts, p.id))
    conn.commit()
    return False


def get_posting(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()


def set_status(
    conn: sqlite3.Connection, posting_id: str, status: str, filter_reason: str | None = None
) -> None:
    conn.execute(
        "UPDATE postings SET status=?, filter_reason=? WHERE id=?",
        (status, filter_reason, posting_id),
    )
    conn.commit()


def pending_for_scoring(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Newest new/score_error postings with fewer than MAX_SCORE_ATTEMPTS attempts."""
    return conn.execute(
        "SELECT * FROM postings WHERE status IN ('new','score_error') AND score_attempts < ?"
        " ORDER BY first_seen_at DESC, rowid DESC LIMIT ?",
        (MAX_SCORE_ATTEMPTS, limit),
    ).fetchall()


def save_score(
    conn: sqlite3.Connection,
    posting_id: str,
    result: ScoreResult,
    model: str,
    prompt_version: str,
    now: datetime,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO scores (posting_id, score, reason, deadline, red_flags, tags,"
            " model, prompt_version, scored_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                posting_id,
                result.score,
                result.reason,
                result.deadline.isoformat() if result.deadline else None,
                json.dumps(result.red_flags),
                json.dumps(result.tags),
                model,
                prompt_version,
                _iso(now),
            ),
        )
        conn.execute(
            "UPDATE postings SET status='scored', score_attempts=score_attempts+1 WHERE id=?",
            (posting_id,),
        )


def record_score_error(conn: sqlite3.Connection, posting_id: str) -> None:
    conn.execute(
        "UPDATE postings SET status='score_error', score_attempts=score_attempts+1 WHERE id=?",
        (posting_id,),
    )
    conn.commit()


def undelivered_scored(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Scored postings never delivered, with score columns joined (best first)."""
    return conn.execute(
        "SELECT p.*, s.score AS score, s.reason AS reason, s.deadline AS llm_deadline,"
        " s.red_flags AS red_flags, s.tags AS tags"
        " FROM postings p JOIN scores s ON s.posting_id=p.id"
        " LEFT JOIN deliveries d ON d.posting_id=p.id"
        " WHERE p.status='scored' AND d.posting_id IS NULL"
        " ORDER BY s.score DESC, p.rowid"
    ).fetchall()


# --- deliveries -----------------------------------------------------------


def record_delivery(
    conn: sqlite3.Connection,
    posting_id: str,
    kind: str,
    chat_id: str,
    message_id: int | None,
    now: datetime,
) -> bool:
    """Record a delivery; returns False (no change) if already delivered."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries (posting_id, kind, chat_id, message_id, sent_at)"
        " VALUES (?,?,?,?,?)",
        (posting_id, kind, str(chat_id), message_id, _iso(now)),
    )
    conn.commit()
    return cur.rowcount == 1


def is_delivered(conn: sqlite3.Connection, posting_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM deliveries WHERE posting_id=?", (posting_id,)).fetchone()
        is not None
    )


# --- reactions ------------------------------------------------------------


def active_reactions(conn: sqlite3.Connection, posting_id: str) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT action, active FROM reactions WHERE posting_id=?", (posting_id,)
    ).fetchall()
    state = {a: False for a in REACTION_ACTIONS}
    state.update({r["action"]: bool(r["active"]) for r in rows})
    return state


def set_reaction(
    conn: sqlite3.Connection, posting_id: str, action: str, now: datetime
) -> dict[str, bool]:
    """Press a reaction button (D-15); returns the resulting active map."""
    if action not in REACTION_ACTIONS:
        raise ValueError(f"unknown reaction action: {action!r}")
    ts = _iso(now)
    current = active_reactions(conn, posting_id)
    new_active = not current[action]

    def put(act: str, active: bool) -> None:
        conn.execute(
            "INSERT INTO reactions (posting_id, action, active, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(posting_id, action) DO UPDATE SET"
            " active=excluded.active, updated_at=excluded.updated_at",
            (posting_id, act, int(active), ts),
        )

    with conn:
        put(action, new_active)
        conn.execute(
            "INSERT INTO reaction_log (posting_id, action, active, at) VALUES (?,?,?,?)",
            (posting_id, action, int(new_active), ts),
        )
        other = _EXCLUSIVE.get(action)
        if new_active and other and current[other]:
            put(other, False)
    return active_reactions(conn, posting_id)


def list_by_reaction(conn: sqlite3.Connection, action: str, limit: int) -> list[sqlite3.Row]:
    """Postings with an active reaction, most recently pressed first."""
    return conn.execute(
        "SELECT p.*, r.updated_at AS reacted_at FROM reactions r"
        " JOIN postings p ON p.id=r.posting_id WHERE r.action=? AND r.active=1"
        " ORDER BY r.updated_at DESC, r.rowid DESC LIMIT ?",
        (action, limit),
    ).fetchall()


def recent_feedback_examples(
    conn: sqlite3.Connection, action: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Active reactions of `action`, newest first, with title/company/tags/score."""
    rows = conn.execute(
        "SELECT p.id, p.title, p.company, p.kind, p.raw_tags, s.score AS score, s.tags AS tags,"
        " r.updated_at AS reacted_at FROM reactions r"
        " JOIN postings p ON p.id=r.posting_id LEFT JOIN scores s ON s.posting_id=p.id"
        " WHERE r.action=? AND r.active=1 ORDER BY r.updated_at DESC, r.rowid DESC LIMIT ?",
        (action, limit),
    ).fetchall()
    return [
        {
            "posting_id": r["id"],
            "title": r["title"],
            "company": r["company"],
            "kind": r["kind"],
            "tags": json.loads(r["tags"] or r["raw_tags"] or "[]"),
            "score": r["score"],
            "reacted_at": r["reacted_at"],
        }
        for r in rows
    ]


# --- state ----------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


# --- source runs ----------------------------------------------------------


def start_source_run(conn: sqlite3.Connection, source: str, now: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO source_runs (source, started_at) VALUES (?,?)", (source, _iso(now))
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def finish_source_run(
    conn: sqlite3.Connection,
    run_id: int,
    now: datetime,
    *,
    ok: bool,
    n_items: int = 0,
    n_new: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE source_runs SET finished_at=?, ok=?, n_items=?, n_new=?, error=? WHERE id=?",
        (_iso(now), int(ok), n_items, n_new, error, run_id),
    )
    conn.commit()


def last_source_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The most recent run of each source, ordered by source name."""
    return conn.execute(
        "SELECT * FROM source_runs WHERE id IN (SELECT MAX(id) FROM source_runs GROUP BY source)"
        " ORDER BY source"
    ).fetchall()


# --- retention ------------------------------------------------------------


def prune(
    conn: sqlite3.Connection,
    now: datetime,
    cache_days: int = 7,
    posting_days: int = 60,
) -> dict[str, int]:
    """Delete stale cache rows and old filtered/duplicate postings; scored rows are kept."""
    from datetime import timedelta

    cache_cut = _iso(now - timedelta(days=cache_days))
    post_cut = _iso(now - timedelta(days=posting_days))
    with conn:
        http = conn.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cache_cut,)).rowcount
        robots = conn.execute(
            "DELETE FROM robots_cache WHERE fetched_at < ?", (cache_cut,)
        ).rowcount
        postings = conn.execute(
            "DELETE FROM postings WHERE status IN ('filtered','duplicate') AND first_seen_at < ?"
            " AND id NOT IN (SELECT posting_id FROM scores)"
            " AND id NOT IN (SELECT posting_id FROM deliveries)"
            " AND id NOT IN (SELECT posting_id FROM reactions)",
            (post_cut,),
        ).rowcount
    return {"http_cache": http, "robots_cache": robots, "postings": postings}
