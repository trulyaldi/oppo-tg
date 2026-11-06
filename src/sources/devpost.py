"""Devpost hackathons source via the public JSON listing (D-31, D-48)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, register
from src.text import html_to_text

API_URL = "https://devpost.com/api/hackathons"
MAX_PAGES = 3
CACHE_TTL_S = 3600.0
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
# "Oct 01 - Nov 30, 2026", "Dec 15, 2026 - Jan 20, 2027", "Dec 28 - 30, 2026", "Nov 30, 2026"
END_RE = re.compile(r"(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+)?(?P<day>\d{1,2}),\s*(?P<year>\d{4})\s*$")
START_MON_RE = re.compile(r"^\s*([A-Za-z]{3})")


def parse_deadline(text: Any) -> date | None:
    """End date of a Devpost `submission_period_dates` range, or None if unparseable."""
    if not isinstance(text, str):
        return None
    m = END_RE.search(text.strip())
    if not m:
        return None
    mon = m.group("mon")
    if mon is None:  # "Dec 28 - 30, 2026": month comes from the start of the range
        sm = START_MON_RE.match(text)
        mon = sm.group(1) if sm else None
    month = MONTHS.get((mon or "").lower())
    if month is None:
        return None
    try:
        return date(int(m.group("year")), month, int(m.group("day")))
    except ValueError:
        return None


def parse_page(data: Any) -> list[Posting]:
    if not isinstance(data, dict) or not isinstance(data.get("hackathons"), list):
        return []
    out: list[Posting] = []
    for h in data["hackathons"]:
        try:
            if not isinstance(h, dict) or not h.get("title") or not h.get("url"):
                continue
            loc = h.get("displayed_location")
            location = str(loc.get("location") or "") if isinstance(loc, dict) else ""
            tags = [
                str(t["name"])
                for t in h.get("themes") or []
                if isinstance(t, dict) and t.get("name")
            ]
            prize = html_to_text(str(h.get("prize_amount") or "")).strip()
            if prize and prize.lstrip("$").strip() not in ("", "0"):
                tags.append(f"prize {prize}")
            period = h.get("submission_period_dates") or "n/a"
            out.append(
                Posting(
                    source="devpost",
                    external_id=str(h["id"]),
                    title=str(h["title"]),
                    company=str(h.get("organization_name") or "Devpost"),
                    url=str(h["url"]),
                    location=location,
                    remote=True if location.strip().lower() == "online" else None,
                    description=f"Hackathon, submission period {period}.",
                    deadline=parse_deadline(h.get("submission_period_dates")),
                    kind=Kind.HACKATHON,
                    raw_tags=tuple(tags),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


@register("devpost")
class DevpostSource:
    name = "devpost"
    description = "Devpost hackathons (open and upcoming)"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("devpost")

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        out: list[Posting] = []
        seen: set[str] = set()
        try:
            for page in range(1, MAX_PAGES + 1):
                data = await ctx.http.get_json(
                    API_URL,
                    params={"status[]": ["upcoming", "open"], "page": str(page)},
                    ttl_s=CACHE_TTL_S,
                )
                items = parse_page(data)
                new = [p for p in items if p.external_id not in seen]
                if not new:
                    break
                seen.update(p.external_id for p in new)
                out.extend(new)
        except Exception:  # noqa: BLE001 - a failing source must not crash the pipeline
            ctx.log.exception("devpost failed")
        return out
