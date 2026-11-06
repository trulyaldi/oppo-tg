"""Hacker News "Who is hiring?" source via the Algolia API (D-31, D-44)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, SourceUnavailable, register
from src.text import html_to_text

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
COMMENT_URL = "https://news.ycombinator.com/item?id={id}"
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)
HIRING_RE = re.compile(r"who\s+is\s+hiring", re.IGNORECASE)
ROLE_RE = re.compile(
    r"\b(?:engineers?|developers?|designers?|scientists?|managers?|analysts?|researchers?|"
    r"intern(?:ship)?s?|lead|architects?|devops|sre|cto|founding|consultants?|"
    r"administrators?|recruiters?|writers?|marketers?|sales|phd|positions?|roles?)\b",
    re.IGNORECASE,
)
NOT_LOCATION_RE = re.compile(
    r"https?://|\bfull[- ]?time\b|\bpart[- ]?time\b|\bcontract\b|\bremote\b|\bonsite\b|"
    r"\bon-site\b|\bhybrid\b|[$€£]\d|\bvisa\b|\bequity\b",
    re.IGNORECASE,
)
_P_RE = re.compile(r"<p\s*>", re.IGNORECASE)
CACHE_TTL_S = 3600.0
TITLE_FALLBACK_LEN = 80


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def pick_story_id(search: Any) -> int | None:
    """Latest "Who is hiring?" story id from a search_by_date payload (ignores other threads)."""
    if not isinstance(search, dict) or not isinstance(search.get("hits"), list):
        return None
    best: tuple[int, int] | None = None
    for hit in search["hits"]:
        try:
            title = str(hit["title"])
            if not HIRING_RE.search(title):
                continue
            key = (int(hit.get("created_at_i") or 0), int(hit["objectID"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if best is None or key > best:
            best = key
    return best[1] if best else None


def parse_header(header: str) -> tuple[str, str, str]:
    """Heuristic `Company | Role | Location | ...` parse -> (company, title, location)."""
    segs = [s.strip() for s in header.split("|") if s.strip()]
    if not segs:
        return "", "", ""
    company = segs[0][:100]
    title = next((s for s in segs[1:] if ROLE_RE.search(s)), "")
    title = title[:200] if title else header.strip()[:TITLE_FALLBACK_LEN]
    location = next(
        (s for s in segs[1:] if s != title and not NOT_LOCATION_RE.search(s) and len(s) <= 120),
        "",
    )
    return company, title, location


def parse_item(data: Any) -> list[Posting]:
    """Map an Algolia story item to Postings, one per non-empty top-level comment."""
    if not isinstance(data, dict) or not isinstance(data.get("children"), list):
        return []
    out: list[Posting] = []
    for c in data["children"]:
        try:
            if not isinstance(c, dict) or not c.get("text"):
                continue
            # HN separates paragraphs with a bare <p>; make the break explicit
            text = html_to_text(_P_RE.sub("<br><p>", str(c["text"])))
            if not text:
                continue
            header = text.splitlines()[0]
            company, title, location = parse_header(header)
            if not company:
                continue
            out.append(
                Posting(
                    source="hn",
                    external_id=str(c["id"]),
                    title=title,
                    company=company,
                    url=COMMENT_URL.format(id=c["id"]),
                    location=location,
                    remote=True if "remote" in header.lower() else None,
                    description=text,
                    posted_at=_parse_dt(c.get("created_at")),
                    kind=Kind.INTERNSHIP if INTERN_RE.search(header) else Kind.JOB,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


@register("hn")
class HnSource:
    name = "hn"
    description = 'Hacker News monthly "Who is hiring?" thread'

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("hn")

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        # FIX-02: a failed request raises SourceUnavailable so run_source records ok=False.
        search = await ctx.http.get_json(
            SEARCH_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": "10"},
            ttl_s=CACHE_TTL_S,
        )
        if search is None:
            raise SourceUnavailable("hn search request failed: no data")
        story_id = pick_story_id(search)
        if story_id is None:
            ctx.log.warning("hn: no 'Who is hiring' story found")
            return []
        data = await ctx.http.get_json(ITEM_URL.format(id=story_id), ttl_s=CACHE_TTL_S)
        if data is None:
            raise SourceUnavailable(f"hn story {story_id} request failed: no data")
        return parse_item(data)
