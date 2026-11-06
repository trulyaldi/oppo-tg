"""Jobicy remote jobs API source (D-31, D-58). Credit: every url is the Jobicy job page."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, register
from src.text import html_to_text

API = "https://jobicy.com/api/jobs"
CACHE_TTL_S = 3600.0  # terms: automated passes no more than once per hour
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)
DEFAULT_INDUSTRIES = ("data-science", "engineering")


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v or "").strip()]


def parse_jobs(data: Any) -> list[Posting]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return []
    out: list[Posting] = []
    for job in jobs:
        try:
            if not isinstance(job, dict):
                continue
            title = str(job.get("jobTitle") or "").strip()
            url = str(job.get("url") or "").strip()
            if not job.get("id") or not title or "jobicy.com/" not in url:
                continue
            types = _as_list(job.get("jobType"))
            level = str(job.get("jobLevel") or "").strip()
            tags = [*_as_list(job.get("jobIndustry")), *types, *([level] if level else [])]
            is_intern = INTERN_RE.search(title) or any(INTERN_RE.search(t) for t in types)
            out.append(
                Posting(
                    source="jobicy",
                    external_id=str(job["id"]),
                    title=title,
                    company=str(job.get("companyName") or "").strip(),
                    url=url,
                    location=str(job.get("jobGeo") or "").strip(),
                    remote=True,
                    description=html_to_text(
                        str(job.get("jobDescription") or job.get("jobExcerpt") or "")
                    ),
                    posted_at=_dt(job.get("pubDate")),
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=tuple(tags),
                )
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def _queries(cfg: AppConfig) -> list[dict[str, str]]:
    section = cfg.source("jobicy")
    industries = section.get("industries")
    industries = list(DEFAULT_INDUSTRIES) if industries is None else industries
    qs = [{"industry": str(i)} for i in industries]
    qs += [{"tag": str(t)} for t in section.get("tags") or []]
    return qs


@register("jobicy")
class JobicySource:
    name = "jobicy"
    description = "Jobicy remote jobs (jobicy.com)"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("jobicy") and bool(_queries(cfg))

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        seen: set[str] = set()
        items: list[Posting] = []
        for query in _queries(ctx.cfg):
            try:
                data = await ctx.http.get_json(
                    API, params={"count": 100, **query}, ttl_s=CACHE_TTL_S
                )
                if data is None:
                    ctx.log.warning("jobicy %s: no data", query)
                    continue
                for p in parse_jobs(data):
                    if p.external_id not in seen:
                        seen.add(p.external_id)
                        items.append(p)
            except Exception:  # noqa: BLE001 - one bad query must not stop the others
                ctx.log.exception("jobicy %s failed", query)
        return items
