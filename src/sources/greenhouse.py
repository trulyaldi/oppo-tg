"""Greenhouse public Job Board API source (D-22, D-31)."""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import RequestTally, SourceContext, register
from src.text import INTERN_RE, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
CACHE_TTL_S = 3600.0
SUMMER_RE = re.compile(r"\bsummer\b", re.IGNORECASE)


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_date(value: Any) -> date | None:
    dt = _parse_dt(value)
    return dt.date() if dt else None


def parse_jobs(data: Any, token: str, company: str) -> list[Posting]:
    """Map a Greenhouse jobs payload to Postings; skips malformed entries."""
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return []
    out: list[Posting] = []
    for job in jobs:
        try:
            title = str(job["title"]).strip()
            if not title:
                continue
            loc = job.get("location")
            location = str(loc.get("name") or "") if isinstance(loc, dict) else ""
            tags = tuple(
                str(d["name"])
                for d in job.get("departments") or []
                if isinstance(d, dict) and d.get("name")
            )
            is_intern = bool(INTERN_RE.search(title) or SUMMER_RE.search(title))
            out.append(
                Posting(
                    source="greenhouse",
                    external_id=f"{token}:{job['id']}",
                    title=title,
                    company=company or str(job.get("company_name") or ""),
                    url=str(job["absolute_url"]),
                    location=location,
                    remote=True if REMOTE_RE.search(location) else None,
                    description=html_to_text(html.unescape(job.get("content") or "")),
                    posted_at=_parse_dt(job.get("updated_at")),
                    deadline=_parse_date(job.get("application_deadline")),
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=tags,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _boards(cfg: AppConfig) -> list[tuple[str, str]]:
    section = cfg.source("greenhouse")
    names = section.get("names") or {}
    result = []
    for token in section.get("boards") or []:
        token = str(token)
        result.append((token, str(names.get(token, ""))))
    return result


@register("greenhouse")
class GreenhouseSource:
    name = "greenhouse"
    family = "ats"
    description = "Greenhouse job boards for a company watchlist"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("greenhouse") and bool(_boards(cfg))

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        tally = RequestTally()
        items: list[Posting] = []
        for token, company in _boards(ctx.cfg):
            try:
                data = await ctx.http.get_json(
                    API.format(token=token), params={"content": "true"}, ttl_s=CACHE_TTL_S
                )
                if data is None:
                    ctx.log.warning("greenhouse board %s: no data", token)
                    tally.failure()
                    continue
                tally.success()
                items.extend(parse_jobs(data, token, company))
            except Exception:  # noqa: BLE001 - one bad board must not stop the others
                ctx.log.exception("greenhouse board %s failed", token)
                tally.failure()
        tally.raise_if_all_failed()
        return items
