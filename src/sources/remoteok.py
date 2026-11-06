"""Remote OK API source (D-178, D-191). Credit: every url is the Remote OK job page."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, SourceUnavailable, register
from src.text import INTERN_RE, html_to_text

API = "https://remoteok.com/api"
CACHE_TTL_S = 3600.0  # one GET per run, never more than hourly (D-178)


def _dt(job: dict[str, Any]) -> datetime | None:
    epoch = job.get("epoch")
    if isinstance(epoch, int | float) and not isinstance(epoch, bool) and epoch > 0:
        try:
            return datetime.fromtimestamp(epoch, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    value = job.get("date")
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return None


def _host_ok(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        scheme = urlparse(url).scheme
    except ValueError:
        return False
    return scheme in ("http", "https") and (
        host == "remoteok.com" or host.endswith(".remoteok.com")
    )


def parse_jobs(data: Any, tags_filter: tuple[str, ...] = ()) -> list[Posting]:
    """Parse the API array. Element 0 is the legal notice and is skipped (D-178)."""
    if not isinstance(data, list):
        return []
    wanted = {t.lower() for t in tags_filter if t}
    out: list[Posting] = []
    for job in data[1:]:
        try:
            if not isinstance(job, dict):
                continue
            title = str(job.get("position") or "").strip()
            url = str(job.get("url") or "").strip()  # never apply_url (D-191)
            if not job.get("id") or not title or not _host_ok(url):
                continue
            raw = job.get("tags")
            tags = (
                [str(t).strip() for t in raw if str(t or "").strip()]
                if isinstance(raw, list)
                else []
            )
            if wanted and not wanted & {t.lower() for t in tags}:
                continue
            published = _dt(job)
            out.append(
                Posting(
                    source="remoteok",
                    external_id=str(job["id"]),
                    title=title,
                    company=str(job.get("company") or "").strip(),
                    url=url,
                    location=str(job.get("location") or "").strip(),
                    remote=True,
                    description=html_to_text(str(job.get("description") or "")),
                    posted_at=published,
                    published_at=published,
                    kind=Kind.INTERNSHIP
                    if INTERN_RE.search(title) or any(INTERN_RE.search(t) for t in tags)
                    else Kind.JOB,
                    raw_tags=tuple(tags),
                )
            )
        except (ValueError, TypeError, AttributeError):
            continue
    return out


@register("remoteok")
class RemoteOKSource:
    name = "remoteok"
    default_tier = "tier2"
    family = "remoteok"
    description = "Remote OK remote jobs (remoteok.com)"
    min_poll_minutes = 60  # D-178

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("remoteok")

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        data = await ctx.http.get_json(API, ttl_s=CACHE_TTL_S)
        if data is None:
            raise SourceUnavailable("remoteok: no data")
        tags = tuple(str(t) for t in ctx.cfg.source("remoteok").get("tags") or [])
        return parse_jobs(data, tags)
