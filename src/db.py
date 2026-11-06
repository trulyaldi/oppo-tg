"""SQLite connection, schema migration and repository functions (PLAN 1.7, D-10)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.models import Posting, ScoreResult
from src.text import content_hash

log = logging.getLogger(__name__)

SCHEMA_VERSION = 11
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


SCHEMA_2 = """
ALTER TABLE scores RENAME COLUMN score TO base_score;
ALTER TABLE scores ADD COLUMN adjusted_score REAL;
ALTER TABLE scores ADD COLUMN employer_region TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE scores ADD COLUMN region_confidence REAL NOT NULL DEFAULT 0;
ALTER TABLE scores ADD COLUMN employer_is_startup INTEGER;
ALTER TABLE scores ADD COLUMN visa_barrier INTEGER;
ALTER TABLE scores ADD COLUMN employer_category TEXT NOT NULL DEFAULT 'unknown';
ALTER TABLE scores ADD COLUMN adjustment REAL NOT NULL DEFAULT 0;
ALTER TABLE scores ADD COLUMN adjustment_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE scores ADD COLUMN weights_version TEXT;
UPDATE scores SET adjusted_score = base_score WHERE adjusted_score IS NULL
"""


SCHEMA_3 = """
CREATE TABLE llm_usage (day TEXT NOT NULL, purpose TEXT NOT NULL, calls INTEGER NOT NULL,
  PRIMARY KEY (day, purpose))
"""


SCHEMA_4 = """
CREATE TABLE cv_versions (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  sha TEXT NOT NULL,
  text TEXT NOT NULL,
  sections TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  parse_model TEXT NOT NULL,
  created_at TEXT NOT NULL
)
"""


SCHEMA_5 = """
CREATE TABLE tailoring_cache (
  posting_id TEXT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  cv_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  removed_claims INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL, prompt_version TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (posting_id, cv_version)
)
"""

SCHEMA_6 = """
CREATE TABLE contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  posting_id TEXT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
  contact_type TEXT NOT NULL,
  type_confidence REAL NOT NULL,
  source_kind TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  UNIQUE (posting_id, name, source_url)
);
CREATE INDEX idx_contacts_expires ON contacts(expires_at)
"""

SCHEMA_7 = """
CREATE TABLE outreach_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  posting_id TEXT NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
  cv_version INTEGER NOT NULL,
  body TEXT NOT NULL, hook TEXT NOT NULL, ask TEXT NOT NULL,
  removed_claims INTEGER NOT NULL DEFAULT 0,
  model TEXT NOT NULL, prompt_version TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE (contact_id, cv_version)
)
"""

_BOARD_SOURCES = "('greenhouse','lever','ashby','rss','telegram_channels')"

SCHEMA_8 = f"""
ALTER TABLE postings ADD COLUMN target TEXT NOT NULL DEFAULT '';
ALTER TABLE postings ADD COLUMN source_published_at TEXT;
ALTER TABLE postings ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE postings ADD COLUMN baseline INTEGER NOT NULL DEFAULT 0;
UPDATE postings SET baseline=1;
UPDATE postings SET target=substr(external_id, 1, instr(external_id, ':') - 1)
  WHERE source IN {_BOARD_SOURCES} AND instr(external_id, ':') > 0;
UPDATE postings SET target=url WHERE source='pagewatch';
CREATE TABLE target_state (
  source TEXT NOT NULL, target TEXT NOT NULL, tier TEXT NOT NULL DEFAULT '',
  baselined_at TEXT, last_attempt_at TEXT, last_ok_at TEXT, last_error TEXT,
  has_validators INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, target)
);
INSERT INTO target_state (source, target, baselined_at, last_ok_at)
  SELECT source, target, MIN(first_seen_at), MAX(last_seen_at) FROM postings
  GROUP BY source, target;
INSERT OR IGNORE INTO target_state (source, target, baselined_at, last_ok_at)
  SELECT 'pagewatch', url, seen_at, seen_at FROM page_snapshots;
INSERT OR IGNORE INTO target_state (source, target, baselined_at, last_ok_at)
  SELECT source, '', MIN(started_at), MAX(started_at) FROM source_runs WHERE ok=1
  AND source NOT IN (SELECT source FROM target_state) GROUP BY source;
CREATE TABLE host_state (
  host TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
  backoff_until TEXT, last_status INTEGER, updated_at TEXT
);
ALTER TABLE deliveries ADD COLUMN latency_s REAL;
CREATE INDEX idx_postings_first_seen ON postings(first_seen_at);
CREATE INDEX idx_postings_source_target ON postings(source, target)
"""

SCHEMA_9 = """
CREATE TABLE board_candidates (
  source TEXT NOT NULL, token TEXT NOT NULL, status TEXT NOT NULL,
  checked_at TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
  discovered_from TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source, token)
)
"""

SCHEMA_10 = """
CREATE TABLE tg_channels (
  handle TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('added','removed')),
  updated_at TEXT NOT NULL
)
"""

SCHEMA_11 = """
ALTER TABLE postings ADD COLUMN hard_blocker TEXT
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with WAL, foreign keys and Row factory."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_MIGRATIONS = (
    SCHEMA_1,
    SCHEMA_2,
    SCHEMA_3,
    SCHEMA_4,
    SCHEMA_5,
    SCHEMA_6,
    SCHEMA_7,
    SCHEMA_8,
    SCHEMA_9,
    SCHEMA_10,
    SCHEMA_11,
)


def _backup_before_v2(conn: sqlite3.Connection) -> None:
    """Copy a file DB to `<name>.pre-v2` with the backup API before the v2 upgrade (D-146)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row else ""
    if not path:
        return
    dest = sqlite3.connect(path + ".pre-v2")
    try:
        conn.backup(dest)
    finally:
        dest.close()


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations; idempotent. Each step is one explicit transaction (D-146)."""
    if conn.in_transaction:
        conn.commit()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if 1 <= version < 2:
        _backup_before_v2(conn)
    old_isolation = conn.isolation_level
    conn.isolation_level = None  # manual BEGIN/COMMIT so DDL is part of the transaction
    try:
        for target, script in enumerate(_MIGRATIONS, 1):
            if version >= target:
                continue
            conn.execute("BEGIN")
            try:
                for stmt in script.split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={target}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = old_isolation


# --- postings -------------------------------------------------------------


class Upsert(StrEnum):
    INSERTED = "inserted"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


def upsert_posting(
    conn: sqlite3.Connection,
    p: Posting,
    fingerprint: str,
    now: datetime,
    *,
    target: str = "",
    baseline: bool = False,
) -> Upsert:
    """Insert, update changed content, or only bump last_seen_at (D-185)."""
    ts = _iso(now)
    chash = content_hash(p)
    published = _iso(p.published_at) if p.published_at else None
    cur = conn.execute(
        "INSERT OR IGNORE INTO postings (id, source, external_id, title, company, url, location,"
        " remote, description, posted_at, deadline, kind, raw_tags, fingerprint,"
        " first_seen_at, last_seen_at, target, source_published_at, content_hash, baseline)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            target,
            published,
            chash,
            int(baseline),
        ),
    )
    if cur.rowcount == 1:
        conn.commit()
        return Upsert.INSERTED
    stored = conn.execute("SELECT content_hash FROM postings WHERE id=?", (p.id,)).fetchone()
    old_hash = stored["content_hash"] if stored else ""
    if old_hash == "":
        conn.execute(
            "UPDATE postings SET last_seen_at=?, content_hash=? WHERE id=?", (ts, chash, p.id)
        )
        outcome = Upsert.UNCHANGED
    elif old_hash != chash:
        conn.execute(
            "UPDATE postings SET last_seen_at=?, title=?, location=?, description=?,"
            " content_hash=?, source_published_at=? WHERE id=?",
            (ts, p.title, p.location, p.description, chash, published, p.id),
        )
        outcome = Upsert.CHANGED
    else:
        conn.execute("UPDATE postings SET last_seen_at=? WHERE id=?", (ts, p.id))
        outcome = Upsert.UNCHANGED
    conn.commit()
    return outcome


def record_target_attempt(
    conn: sqlite3.Connection,
    source: str,
    target: str,
    now: datetime,
    *,
    ok: bool,
    error: str | None = None,
    tier: str = "",
    has_validators: bool | None = None,
) -> None:
    """Update target_state after a fetch; baselined_at is never touched here."""
    ts = _iso(now)
    hv = None if has_validators is None else int(has_validators)
    conn.execute(
        "INSERT INTO target_state (source, target, tier, last_attempt_at, last_ok_at,"
        " last_error, has_validators) VALUES (?,?,?,?,?,?,COALESCE(?,0))"
        " ON CONFLICT(source, target) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,"
        " last_ok_at=CASE WHEN ? THEN excluded.last_attempt_at ELSE last_ok_at END,"
        " last_error=excluded.last_error,"
        " tier=CASE WHEN excluded.tier != '' THEN excluded.tier ELSE tier END,"
        " has_validators=COALESCE(?, has_validators)",
        (
            source,
            target,
            tier,
            ts,
            ts if ok else None,
            None if ok else (error or "error"),
            hv,
            int(ok),
            hv,
        ),
    )
    conn.commit()


def mark_baselined(conn: sqlite3.Connection, source: str, target: str, now: datetime) -> None:
    """Set baselined_at once, after the first ok fetch of a target (D-188)."""
    conn.execute(
        "UPDATE target_state SET baselined_at=? WHERE source=? AND target=?"
        " AND baselined_at IS NULL",
        (_iso(now), source, target),
    )
    conn.commit()


def get_target_state(conn: sqlite3.Connection, source: str, target: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM target_state WHERE source=? AND target=?", (source, target)
    ).fetchone()


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
    """New/score_error postings under MAX_SCORE_ATTEMPTS: non-baseline first, then newest."""
    return conn.execute(
        "SELECT * FROM postings WHERE status IN ('new','score_error') AND score_attempts < ?"
        " ORDER BY baseline ASC, first_seen_at DESC, rowid DESC LIMIT ?",
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
            "INSERT OR REPLACE INTO scores (posting_id, base_score, adjusted_score, reason,"
            " deadline, red_flags, tags, model, prompt_version, scored_at, weights_version,"
            " employer_region, region_confidence, employer_is_startup, visa_barrier)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (
                posting_id,
                result.score,
                result.score,
                result.reason,
                result.deadline.isoformat() if result.deadline else None,
                json.dumps(result.red_flags),
                json.dumps(result.tags),
                model,
                prompt_version,
                _iso(now),
                result.employer_region,
                result.region_confidence,
                None if result.employer_is_startup is None else int(result.employer_is_startup),
                None if result.visa_barrier is None else int(result.visa_barrier),
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
        "SELECT p.*, s.base_score AS base_score, s.adjusted_score AS adjusted_score,"
        " s.adjusted_score AS score, s.employer_category AS employer_category,"
        " s.adjustment_detail AS adjustment_detail, s.reason AS reason,"
        " s.deadline AS llm_deadline, s.red_flags AS red_flags, s.tags AS tags"
        " FROM postings p JOIN scores s ON s.posting_id=p.id"
        " LEFT JOIN deliveries d ON d.posting_id=p.id"
        " WHERE p.status='scored' AND d.posting_id IS NULL"
        " ORDER BY s.adjusted_score DESC, p.rowid"
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
    """Record a delivery with its latency since first sight (D-210); False if already delivered."""
    row = conn.execute(
        "SELECT p.source, p.target, p.first_seen_at, ts.tier FROM postings p"
        " LEFT JOIN target_state ts ON ts.source=p.source AND ts.target=p.target"
        " WHERE p.id=?",
        (posting_id,),
    ).fetchone()
    latency: float | None = None
    if row is not None:
        latency = (now - datetime.fromisoformat(row["first_seen_at"])).total_seconds()
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries (posting_id, kind, chat_id, message_id, sent_at,"
        " latency_s) VALUES (?,?,?,?,?,?)",
        (posting_id, kind, str(chat_id), message_id, _iso(now), latency),
    )
    conn.commit()
    if cur.rowcount == 1 and row is not None:
        log.info(
            "delivery kind=%s source=%s tier=%s latency_s=%s",
            kind,
            row["source"],
            row["tier"] or "unknown",
            f"{latency:.0f}" if latency is not None else "n/a",
        )
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
        "SELECT p.id, p.title, p.company, p.kind, p.raw_tags, s.base_score AS score,"
        " s.tags AS tags, r.updated_at AS reacted_at FROM reactions r"
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


def tg_channel_overlay(conn: sqlite3.Connection) -> dict[str, str]:
    """Runtime Telegram channel edits as {handle: 'added'|'removed'} (D-193)."""
    return {r["handle"]: r["state"] for r in conn.execute("SELECT handle, state FROM tg_channels")}


def set_tg_channel(conn: sqlite3.Connection, handle: str, state: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO tg_channels (handle, state, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(handle) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (handle, state, _iso(now)),
    )
    conn.commit()


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
            "DELETE FROM postings WHERE status IN ('filtered','duplicate') AND last_seen_at < ?"
            " AND id NOT IN (SELECT posting_id FROM scores)"
            " AND id NOT IN (SELECT posting_id FROM deliveries)"
            " AND id NOT IN (SELECT posting_id FROM reactions)",
            (post_cut,),
        ).rowcount
    from src.contacts.store import prune_contacts

    contacts = prune_contacts(conn, now)
    return {"http_cache": http, "robots_cache": robots, "postings": postings, "contacts": contacts}
