"""On-demand contact discovery for one posting (N6, D-103, D-105).

Called only from the /prepare handler. Never from collect, the scheduler or alerts.
Page fetches go through PoliteClient (robots, rate limit, 7-day cache, blocked hosts).
Nothing here logs contact fields, only counts.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from src import db
from src.clock import Clock
from src.config import AppConfig, OutreachConfig
from src.contacts.parsers.hn import fetch_hn_contacts
from src.contacts.parsers.posting_text import parse_posting_contacts
from src.contacts.parsers.team_page import parse_team_page
from src.contacts.store import RawContact, add_contact
from src.http import is_blocked_host, is_unsafe_host
from src.llm.budget import TZ
from src.ranking.employer import normalize_company
from src.ranking.weights import Weights

log = logging.getLogger(__name__)

PAGE_TTL_S = 7 * 24 * 3600
TEAM_PATHS = ("/team", "/about")
EVENT_KINDS = ("hackathon", "competition")
# D-103: ATS, aggregator and event-listing hosts are never treated as the employer's domain.
AGGREGATOR_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "news.ycombinator.com",
    "hn.algolia.com",
    "jobicy.com",
    "arbeitnow.com",
    "remoteok.com",
    "workable.com",
    "hh.kz",
    "hh.ru",
    "t.me",
    "mlh.com",
    "mlh.io",
)
HN_SOURCES = ("hn",)


@dataclass(frozen=True)
class DiscoveryReport:
    added: int = 0
    unreachable: int = 0


def is_aggregator_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in AGGREGATOR_HOSTS)


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def watchlist_domain(company: str, weights: Weights) -> str | None:
    norm = normalize_company(company)
    if not norm:
        return None
    for e in (*weights.intl_top_tier, *weights.kz_employers):
        if norm in {normalize_company(n) for n in (e.name, *e.aliases)}:
            return e.domain or None
    return None


def employer_host(posting: Any, weights: Weights) -> str | None:
    """Watchlist domain, else the posting URL host unless it is an aggregator/blocked host."""
    dom = watchlist_domain(posting["company"], weights)
    if dom:
        host = _host(f"https://{dom.strip().lstrip('/')}")
        if host:
            return host
    host = _host(posting["url"])
    if not host or is_aggregator_host(host) or is_blocked_host(f"https://{host}/"):
        return None
    return host


def _default_port(url: str) -> bool:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return port is None or (port, parts.scheme) in ((80, "http"), (443, "https"))


def safe_target(url: str) -> bool:
    """FIX-24: no localhost, IP literals or explicit non-default ports (D-149)."""
    return not is_unsafe_host(url) and _default_port(url)


def _day(clock: Clock) -> str:
    return clock.now().astimezone(TZ).date().isoformat()


def _recently_discovered(conn: sqlite3.Connection, pid: str, now: datetime, days: int) -> bool:
    raw = db.get_state(conn, f"contacts:discovered:{pid}")
    if not raw:
        return False
    try:
        return now - datetime.fromisoformat(raw) < timedelta(days=days)
    except ValueError:
        return False


async def discover_contacts(
    conn: sqlite3.Connection,
    http: Any,
    posting_row: Any,
    cfg: AppConfig,
    clock: Clock,
    weights: Weights,
) -> DiscoveryReport:
    oc = OutreachConfig.from_cfg(cfg)
    pid = posting_row["id"]
    now = clock.now()
    if _recently_discovered(conn, pid, now, oc.rediscover_after_days):
        return DiscoveryReport()

    added = 0
    unreachable = 0
    company = posting_row["company"] or ""
    post_url = posting_row["url"] or ""

    def store(raws: list[RawContact], kind: str, url: str) -> int:
        n = 0
        for raw in raws:
            cid = add_contact(
                conn,
                pid,
                raw,
                source_kind=kind,
                source_url=url,
                company=company,
                now=now,
                ttl_days=oc.contact_ttl_days,
                max_per_posting=oc.max_contacts_per_posting,
            )
            if cid is not None:
                n += 1
        return n

    # 1. posting text, offline
    text = f"{posting_row['title'] or ''}\n{posting_row['description'] or ''}"
    added += store(parse_posting_contacts(text), "posting_text", post_url)

    # 2. HN comment (one cached request)
    if posting_row["source"] in HN_SOURCES and http is not None:
        raws = await fetch_hn_contacts(http, posting_row["external_id"])
        added += store(
            raws, "hn", f"https://news.ycombinator.com/item?id={posting_row['external_id']}"
        )

    # 3./4. pages, at most max_page_fetches_per_prepare, and the daily cap
    stamp = True
    if http is None:
        stamp = False
    else:
        raw_targets = _page_targets(posting_row, weights)
        targets = [t for t in raw_targets if safe_target(t[0])]
        unreachable += len(raw_targets) - len(targets)
        day_key = f"contacts:pages:{_day(clock)}"
        used_today = int(db.get_state(conn, day_key, "0") or 0)
        fetched = 0
        for url, kind in targets:
            if fetched >= oc.max_page_fetches_per_prepare:
                break
            if used_today >= oc.max_page_fetches_per_day:
                stamp = False
                break
            fetched += 1
            used_today += 1
            db.set_state(conn, day_key, str(used_today))
            host = _host(url)
            denied = getattr(http, "denied", None)
            if denied is not None:
                denied.discard(host)
            try:
                html = await http.get_text(url, ttl_s=PAGE_TTL_S)
            except Exception:  # noqa: BLE001 - discovery must not break /prepare
                log.warning("contact page fetch failed")
                html = None
            if html is None:
                unreachable += 1
                if denied is not None and host in denied:
                    break  # 403/429: stop, never evade (D-105)
                continue
            found = store(parse_team_page(html, url), kind, url)
            added += found
            if found:
                break

    if stamp:
        db.set_state(conn, f"contacts:discovered:{pid}", now.isoformat())
    log.info("contact discovery for %s: added %d, unreachable %d", pid, added, unreachable)
    return DiscoveryReport(added=added, unreachable=unreachable)


def _page_targets(posting_row: Any, weights: Weights) -> list[tuple[str, str]]:
    """Ordered (url, source_kind) candidates; the caller applies the fetch caps."""
    out: list[tuple[str, str]] = []
    post_url = posting_row["url"] or ""
    post_host = _host(post_url)
    if str(posting_row["kind"]) in EVENT_KINDS:
        if (
            post_host
            and post_url.startswith(("http://", "https://"))
            and not is_aggregator_host(post_host)
            and not is_blocked_host(post_url)
        ):
            out.append((post_url, "organizer_page"))
            out.append((f"https://{post_host}/team", "team_page"))
        return out
    host = employer_host(posting_row, weights)
    if host:
        out += [(f"https://{host}{p}", "team_page") for p in TEAM_PATHS]
    return out
