"""Telegram channel discovery (D-256, KF-22).

Searches public channels with `contacts.SearchRequest`, samples a few recent posts of each new
candidate and stores it as `new` in `tg_channel_candidates`. It never joins, sends or marks
anything read: the client is wrapped in `ReadOnlyTelegram`. A channel is added only by the
user's button press (KF-23). Sample post text is untrusted: it is stored raw, clipped to 200
characters, and escaped when rendered. No post text is ever logged.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src import db
from src.clock import Clock
from src.config import AppConfig, TelegramDiscoveryConfig
from src.cues import Cues
from src.discovery.telegram_budget import discovery_budget
from src.sources.telegram_channels import channels, flood_gate, normalize_channel, set_flood
from src.sources.telegram_ro import ReadOnlyViolation, read_only

log = logging.getLogger(__name__)

MIN_QUERY_LEN = 4
SAMPLE_CLIP = 200
NEXT_QUERY_KEY = "tg_discovery:next_query"


@dataclass
class Candidate:
    handle: str
    title: str | None
    subscribers: int | None
    job_ratio: float
    samples: list[str]
    found_by: str


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    calls: int = 0
    stopped: str | None = None  # "cap", "flood" or None


def cue_queries(cues: Cues) -> list[str]:
    """Plain discovery queries from the cue file, 4 characters or more (D-244)."""
    group = cues.groups.get("telegram_discovery")
    terms = group.terms(plain_only=True) if group else []
    return [t for t in terms if len(t.strip()) >= MIN_QUERY_LEN]


def _pick_queries(
    conn: sqlite3.Connection, queries: list[str] | None, cues: Cues, n: int
) -> list[str]:
    if queries is not None:
        return [q.strip() for q in queries if len(q.strip()) >= MIN_QUERY_LEN][:n]
    pool = cue_queries(cues)
    if not pool:
        return []
    try:
        start = int(db.get_state(conn, NEXT_QUERY_KEY, "0") or 0) % len(pool)
    except ValueError:
        start = 0
    picked = [pool[(start + i) % len(pool)] for i in range(min(n, len(pool)))]
    db.set_state(conn, NEXT_QUERY_KEY, str((start + len(picked)) % len(pool)))
    return picked


def _is_candidate_channel(chat: Any) -> bool:
    """A broadcast channel (not a megagroup) that has a public username."""
    return (
        bool(getattr(chat, "broadcast", False))
        and not getattr(chat, "megagroup", False)
        and normalize_channel(getattr(chat, "username", None)) is not None
    )


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _is_flood(exc: BaseException) -> bool:
    return type(exc).__name__ == "FloodWaitError"


async def discover_channels(
    conn: sqlite3.Connection,
    client: Any,
    cues: Cues,
    cfg: AppConfig,
    clock: Clock,
    queries: list[str] | None = None,
) -> DiscoveryResult:
    """Run one discovery pass and return the candidates stored by it."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.contacts import SearchRequest

    dcfg = TelegramDiscoveryConfig.from_cfg(cfg)
    client = read_only(client)
    budget = discovery_budget(conn, clock, dcfg)
    result = DiscoveryResult()
    skip = {h.casefold() for h in channels(cfg, conn)}
    skip |= {h.casefold() for h in db.tg_channel_overlay(conn)}

    async def call(purpose: str, fn: Any) -> Any:
        """One gated, counted Telethon call; None when stopped by flood or the cap."""
        if flood_gate(conn, clock.now(), log, "telegram discovery"):
            result.stopped = "flood"
            return None
        if not budget.reserve(purpose):
            result.stopped = "cap"
            return None
        result.calls += 1
        try:
            return await fn()
        except ReadOnlyViolation:
            raise
        except Exception as exc:
            if _is_flood(exc):
                set_flood(conn, clock.now(), getattr(exc, "seconds", 60))
                log.warning(
                    "telegram discovery: flood wait %ss, stopping", getattr(exc, "seconds", "?")
                )
                result.stopped = "flood"
            else:
                log.warning("telegram discovery: %s failed (%s)", purpose, type(exc).__name__)
            return _FAILED

    for query in _pick_queries(conn, queries, cues, dcfg.max_queries_per_run):
        found = await call(
            "tg_search",
            lambda q=query: client(SearchRequest(q=q, limit=dcfg.max_candidates_per_query)),
        )
        if found is None or found is _FAILED:
            if result.stopped:
                break
            continue
        for chat in list(getattr(found, "chats", None) or [])[: dcfg.max_candidates_per_query]:
            if not _is_candidate_channel(chat):
                continue
            handle = chat.username
            key = handle.casefold()
            if key in skip or db.tg_candidate_known(conn, handle):
                continue
            cand = await _sample(chat, query, call, client, cues, dcfg, GetFullChannelRequest)
            if result.stopped:
                break
            if cand is None:
                continue
            skip.add(key)
            if db.add_tg_candidate(
                conn,
                cand.handle,
                title=cand.title,
                subscribers=cand.subscribers,
                job_ratio=cand.job_ratio,
                samples=cand.samples,
                found_by=cand.found_by,
                now=clock.now(),
            ):
                result.candidates.append(cand)
        if result.stopped:
            break
    return result


_FAILED = object()


async def _sample(
    chat: Any,
    query: str,
    call: Any,
    client: Any,
    cues: Cues,
    dcfg: TelegramDiscoveryConfig,
    full_request: Any,
) -> Candidate | None:
    subscribers = _int_or_none(getattr(chat, "participants_count", None))
    if subscribers is None:
        full = await call("tg_sample", lambda: client(full_request(chat)))
        if full is None:
            return None
        if full is not _FAILED:
            subscribers = _int_or_none(
                getattr(getattr(full, "full_chat", None), "participants_count", None)
            )

    async def read() -> list[Any]:
        return [m async for m in client.iter_messages(chat, limit=dcfg.sample_window)]

    messages = await call("tg_sample", read)
    if messages is None or messages is _FAILED:
        return None
    texts = [
        t.strip()
        for t in (getattr(m, "message", None) for m in messages)
        if isinstance(t, str) and t.strip()
    ]
    if not texts:
        return None
    ratio = sum(1 for t in texts if cues.is_positive(t)) / len(texts)
    samples = [t[:SAMPLE_CLIP] for t in texts[: dcfg.sample_posts]]
    return Candidate(
        handle=chat.username,
        title=getattr(chat, "title", None),
        subscribers=subscribers,
        job_ratio=round(ratio, 4),
        samples=samples,
        found_by=query,
    )
