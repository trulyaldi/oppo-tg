"""Validate ATS boards seen in collected postings and add the real ones (D-190, FC-16).

Only already-collected data is scanned. The extracted URL is never fetched: each
check is one polite request to the canonical API URL rebuilt from the token by the
board source's own `validate`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.discovery import store
from src.discovery.extract import BoardRef, find_board_refs
from src.http import PoliteClient
from src.sources.base import REGISTRY, SourceContext, ValidationUnavailable

log = logging.getLogger(__name__)

CURSOR_KEY = "discovery:last_rowid"
SCAN_LIMIT = 5000


@dataclass
class DiscoveryStats:
    scanned: int = 0
    validated: int = 0
    added: int = 0
    invalid: int = 0
    errors: int = 0
    skipped: int = 0
    added_boards: list[tuple[str, str]] = field(default_factory=list)


def _validator(cfg: AppConfig, source: str):
    """The board source's `validate`, or None if it is unregistered or disabled."""
    factory = REGISTRY.get(source)
    if factory is None or not cfg.source_enabled(source):
        return None
    src = factory()
    return src if callable(getattr(src, "validate", None)) else None


def _config_tokens(ctx: SourceContext, src) -> set[str]:
    return {t.key.casefold() for t in src.targets(ctx) if t.origin == "config"}


def _today_counts(conn: sqlite3.Connection, day: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(status='valid'), 0) FROM board_candidates"
        " WHERE substr(checked_at, 1, 10)=?",
        (day,),
    ).fetchone()
    return int(row[0]), int(row[1])


def _record(
    conn: sqlite3.Connection,
    ref: BoardRef,
    status: str,
    now: datetime,
    company: str,
    origin: str,
) -> None:
    conn.execute(
        "INSERT INTO board_candidates (source, token, status, checked_at, company,"
        " discovered_from) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source, token) DO UPDATE SET status=excluded.status,"
        " checked_at=excluded.checked_at, company=excluded.company,"
        " discovered_from=excluded.discovered_from",
        (ref.source, ref.token, status, now.isoformat(), company[: store.MAX_COMPANY_LEN], origin),
    )
    conn.commit()


def _skip_cached(conn: sqlite3.Connection, ref: BoardRef, now: datetime, cache_days: int) -> bool:
    row = conn.execute(
        "SELECT status, checked_at FROM board_candidates WHERE source=? AND token=? COLLATE NOCASE",
        (ref.source, ref.token),
    ).fetchone()
    if row is None:
        return False
    if row["status"] == "valid":
        return True
    try:
        checked = datetime.fromisoformat(row["checked_at"])
    except ValueError:
        return False
    if row["status"] == "invalid":
        return now - checked < timedelta(days=cache_days)
    return checked.astimezone(UTC).date() == now.astimezone(UTC).date()  # error: retry tomorrow


async def run_board_discovery(
    conn: sqlite3.Connection,
    http: PoliteClient,
    cfg: AppConfig,
    clock: Clock,
    settings: Settings | None = None,
    lock_factory=None,
) -> DiscoveryStats:
    """Scan new postings for ATS board links, validate a few, add the real ones.

    Validation requests run outside the pipeline lock; only the DB and file writes
    take it (`lock_factory` returns an async context manager, default PIPELINE_LOCK).
    """
    stats = DiscoveryStats()
    dcfg = cfg.discovery
    if not dcfg.enabled:
        return stats
    if lock_factory is None:
        from src.pipeline.run import pipeline_lock as lock_factory
    settings = settings or Settings.from_env({}, require_secrets=False)
    ctx = SourceContext(http=http, settings=settings, cfg=cfg, clock=clock)
    now = clock.now()
    day = now.astimezone(UTC).date().isoformat()
    validations, new_boards = _today_counts(conn, day)

    cursor = int(db.get_state(conn, CURSOR_KEY, "0") or 0)
    rows = conn.execute(
        "SELECT rowid AS rid, id, url, description FROM postings WHERE rowid>?"
        " ORDER BY rowid LIMIT ?",
        (cursor, SCAN_LIMIT),
    ).fetchall()
    sources: dict[str, object | None] = {}
    configured: dict[str, set[str]] = {}
    known = {(e.source, e.token.casefold()) for e in store.load(dcfg.boards_file)}
    seen: set[tuple[str, str]] = set()
    new_cursor = cursor
    for row in rows:
        stop = False
        refs = find_board_refs(row["url"] or "") + find_board_refs(row["description"] or "")
        for ref in refs:
            key = (ref.source, ref.token.casefold())
            if key in seen:
                continue
            seen.add(key)
            if ref.source not in sources:
                src = _validator(cfg, ref.source)
                sources[ref.source] = src
                configured[ref.source] = _config_tokens(ctx, src) if src else set()
            src = sources[ref.source]
            if (
                src is None  # unregistered or disabled: not cached as invalid
                or key in known
                or ref.token.casefold() in configured[ref.source]
                or _skip_cached(conn, ref, now, dcfg.negative_cache_days)
            ):
                stats.skipped += 1
                continue
            if validations >= dcfg.max_validations_per_day or (
                new_boards >= dcfg.max_new_boards_per_day
            ):
                stop = True
                break
            validations += 1
            stats.validated += 1
            origin = f"posting:{row['id']}"
            try:
                ok, company = await src.validate(ctx, ref.token)  # type: ignore[attr-defined]
            except ValidationUnavailable as exc:
                log.info("board %s:%s not checkable now: %s", ref.source, ref.token, exc)
                _record(conn, ref, "error", now, "", origin)
                stats.errors += 1
                continue
            if not ok:
                _record(conn, ref, "invalid", now, "", origin)
                stats.invalid += 1
                continue
            async with lock_factory():
                try:
                    store.add(dcfg.boards_file, ref.source, ref.token, company, now)
                except store.StoreCorrupt:
                    log.warning("boards file corrupt; not adding %s:%s", ref.source, ref.token)
                    stats.errors += 1
                    continue
                _record(conn, ref, "valid", now, company, origin)
            new_boards += 1
            stats.added += 1
            stats.added_boards.append((ref.source, ref.token))
            known.add(key)
        if stop:
            break
        new_cursor = row["rid"]
        stats.scanned += 1
    db.set_state(conn, CURSOR_KEY, str(new_cursor))
    conn.commit()
    log.info("board discovery: %s", stats)
    return stats


__all__ = ["DiscoveryStats", "run_board_discovery"]
