"""MLH season events source: parses the public season schedule page (D-31, D-49)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, register

CACHE_TTL_S = 3600.0
EVENT_TYPE = "https://schema.org/Event"


def season_year(today: date) -> int:
    """MLH seasons run roughly Jul-Jun and are named by the ending year."""
    return today.year + 1 if today.month >= 7 else today.year


def season_url(today: date) -> str:
    return f"https://mlh.io/seasons/{season_year(today)}/events"


def _meta(card: Tag, prop: str) -> str:
    m = card.find("meta", attrs={"itemprop": prop})
    return str(m.get("content") or "").strip() if isinstance(m, Tag) else ""


def _parse_date(text: str) -> date | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clean_url(url: str) -> str:
    return url.split("?", 1)[0]


def parse_events(html: Any, today: date | None = None) -> list[Posting]:
    """Event cards -> postings. Events that already ended before `today` are skipped."""
    if not isinstance(html, str) or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[Posting] = []
    for card in soup.find_all("a", attrs={"itemtype": EVENT_TYPE}):
        try:
            name_el = card.find("h4")
            name = name_el.get_text(" ", strip=True) if name_el else ""
            href = str(card.get("href") or "") or _meta(card, "url")
            if not name or not href.startswith(("http://", "https://")):
                continue
            start = _parse_date(_meta(card, "startDate"))
            end = _parse_date(_meta(card, "endDate")) or start
            if today is not None and end is not None and end < today:
                continue
            labels = [
                str(s.get("title")).strip()
                for s in card.find_all("span", attrs={"title": True})
                if str(s.get("title")).strip()
            ]
            place = ", ".join(
                p
                for p in (
                    _meta(card, "addressLocality"),
                    _meta(card, "addressRegion"),
                    _meta(card, "addressCountry"),
                )
                if p
            )
            remote = True if "Digital" in labels else False if "In-Person" in labels else None
            dates = f"{start.isoformat()} to {end.isoformat()}" if start and end else "dates n/a"
            url = _clean_url(href)
            out.append(
                Posting(
                    source="mlh",
                    external_id=_clean_url(_meta(card, "url") or href),
                    title=name,
                    company="Major League Hacking",
                    url=url,
                    location=place,
                    remote=remote,
                    description=f"MLH hackathon, {dates}. {', '.join(labels)}".strip(),
                    deadline=start,
                    kind=Kind.HACKATHON,
                    raw_tags=tuple(labels),
                )
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


@register("mlh")
class MLHSource:
    name = "mlh"
    description = "MLH hackathons (current season)"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("mlh")

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        try:
            today = ctx.clock.now().astimezone(ZoneInfo(ctx.settings.timezone)).date()
            html = await ctx.http.get_text(season_url(today), ttl_s=CACHE_TTL_S)
            items = parse_events(html, today)
            if not items:
                ctx.log.warning("mlh: no event cards parsed (markup changed or season empty)")
            return items
        except Exception:  # noqa: BLE001 - a failing source must not crash the pipeline
            ctx.log.exception("mlh failed")
            return []
