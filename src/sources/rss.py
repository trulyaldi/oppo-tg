"""RSS/Atom feeds source: fellowships, research programs, job feeds (D-31, D-45)."""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime
from typing import Any

import feedparser

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import RequestTally, SourceContext, Target, fetch_all_targets, register
from src.text import INTERN_RE, html_to_text

DEADLINE_RE = re.compile(r"deadline(?: date)?:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", re.IGNORECASE)
CACHE_TTL_S = 3600.0


def _dt(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime.fromtimestamp(calendar.timegm(tm), tz=UTC)
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def _deadline(text: str) -> date | None:
    m = DEADLINE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%B %d, %Y").date()
    except ValueError:
        return None


def _kind(value: str) -> Kind:
    try:
        return Kind(value)
    except ValueError:
        return Kind.OTHER


def parse_feed(text: str, name: str, kind: str) -> list[Posting]:
    """Parse RSS/Atom text; skips entries without a title or http(s) link. Garbage -> []."""
    try:
        parsed = feedparser.parse(text)
    except Exception:  # noqa: BLE001 - feedparser must never crash the source
        return []
    base_kind = _kind(kind)
    out: list[Posting] = []
    for entry in parsed.entries:
        try:
            link = str(entry.get("link") or "").strip()
            title = html_to_text(str(entry.get("title") or "")).strip()
            if not link or not title:
                continue
            desc_raw = str(entry.get("summary") or entry.get("description") or "")
            description = html_to_text(desc_raw)
            ext = str(entry.get("id") or link)
            is_intern = base_kind == Kind.JOB and INTERN_RE.search(title)
            tags = tuple(str(t["term"]) for t in entry.get("tags") or [] if t.get("term"))
            out.append(
                Posting(
                    source="rss",
                    external_id=f"{name}:{ext}",
                    title=title,
                    company=str(entry.get("author") or "") or name,
                    url=link,
                    description=description,
                    posted_at=_dt(entry),
                    published_at=_dt(entry),
                    deadline=_deadline(description),
                    kind=Kind.INTERNSHIP if is_intern else base_kind,
                    raw_tags=tags,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _feeds(cfg: AppConfig) -> list[dict[str, str]]:
    feeds = cfg.source("rss").get("feeds") or []
    return [
        {
            "url": str(f["url"]),
            "name": str(f.get("name") or f["url"]),
            "kind": str(f.get("kind", "")),
        }
        for f in feeds
        if isinstance(f, dict) and f.get("url")
    ]


@register("rss")
class RssSource:
    name = "rss"
    description = "RSS/Atom feeds (fellowships, research programs, job feeds)"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("rss") and bool(_feeds(cfg))

    def targets(self, ctx: SourceContext) -> list[Target]:
        seen: set[str] = set()
        out = []
        for feed in _feeds(ctx.cfg):
            if feed["name"] not in seen:
                seen.add(feed["name"])
                out.append(Target("rss", feed["name"]))
        return out

    async def fetch_target(self, ctx: SourceContext, target: Target) -> list[Posting]:
        # Feeds sharing a name form one target (the key is the feed name); one bad
        # feed of the group does not hide the others, only all failing fails the target.
        tally = RequestTally()
        items: list[Posting] = []
        for feed in (f for f in _feeds(ctx.cfg) if f["name"] == target.key):
            try:
                # Never let feedparser fetch: it would bypass robots/rate limit/cache.
                text = await ctx.http.get_text(feed["url"], ttl_s=CACHE_TTL_S)
                if text is None:
                    ctx.log.warning("rss feed %s: no data", feed["name"])
                    tally.failure()
                    continue
                tally.success()
                items.extend(parse_feed(text, feed["name"], feed["kind"]))
            except Exception:  # noqa: BLE001
                ctx.log.exception("rss feed %s failed", feed["name"])
                tally.failure()
        tally.raise_if_all_failed()
        return items

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        return await fetch_all_targets(self, ctx)
