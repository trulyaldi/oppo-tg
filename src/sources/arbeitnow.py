"""Arbeitnow job board API source (D-238). Credit: every url is the Arbeitnow job page.

Terms (s.11) ask for a link back, so items link to arbeitnow.com and render "via Arbeitnow".
The API refreshes hourly, so it is polled at most hourly, page 1 only by default.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, SourceUnavailable, register
from src.text import INTERN_RE, html_to_text

API = "https://www.arbeitnow.com/api/job-board-api"
HOST = "www.arbeitnow.com"
CACHE_TTL_S = 3600.0  # the API updates hourly (D-238)
DEFAULT_MAX_PAGES = 1


def _dt(value: Any) -> datetime | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _strs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


def _description(raw: str) -> str:
    if "&lt;" in raw and "<" not in raw:  # entity-encoded HTML fragment
        raw = html.unescape(raw)
    return html_to_text(raw)


def parse_jobs(data: Any) -> list[Posting]:
    jobs = data.get("data") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return []
    out: list[Posting] = []
    for job in jobs:
        try:
            if not isinstance(job, dict):
                continue
            slug = str(job.get("slug") or "").strip()
            title = str(job.get("title") or "").strip()
            url = str(job.get("url") or "").strip()
            if not slug or not title or urlparse(url).hostname != HOST:
                continue
            types = _strs(job.get("job_types"))
            is_intern = INTERN_RE.search(title) or any(INTERN_RE.search(t) for t in types)
            when = _dt(job.get("created_at"))
            out.append(
                Posting(
                    source="arbeitnow",
                    external_id=slug,
                    title=title,
                    company=str(job.get("company_name") or "").strip(),
                    url=url,
                    location=str(job.get("location") or "").strip(),
                    remote=job.get("remote") is True,
                    description=_description(str(job.get("description") or "")),
                    posted_at=when,
                    published_at=when,
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=tuple([*_strs(job.get("tags")), *types]),
                )
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


@register("arbeitnow")
class ArbeitnowSource:
    name = "arbeitnow"
    default_tier = "tier2"
    family = "arbeitnow"
    description = "Arbeitnow jobs (arbeitnow.com)"
    min_poll_minutes = 60  # D-238

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("arbeitnow")

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        pages = int(ctx.cfg.source("arbeitnow").get("max_pages", DEFAULT_MAX_PAGES))
        seen: set[str] = set()
        items: list[Posting] = []
        for page in range(1, pages + 1):
            params = {"page": page} if page > 1 else None
            data = await ctx.http.get_json(API, params=params, ttl_s=CACHE_TTL_S)
            if data is None:
                if page == 1:
                    raise SourceUnavailable("arbeitnow: no data")
                ctx.note_partial("arbeitnow")
                break
            for p in parse_jobs(data):
                if p.external_id not in seen:
                    seen.add(p.external_id)
                    items.append(p)
        return items
