"""Lever public Postings API source (D-31, D-42)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import (
    SourceContext,
    SourceUnavailable,
    Target,
    discovered_targets,
    fetch_all_targets,
    fetch_board_json,
    register,
)
from src.text import INTERN_RE, html_to_text

API = "https://api.lever.co/v0/postings/{company}"
REMOTE_RE = re.compile(r"\bremote\b", re.IGNORECASE)
CACHE_TTL_S = 3600.0


def _ms_to_dt(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_postings(data: Any, company_slug: str, company: str) -> list[Posting]:
    """Map a Lever postings payload (a JSON list) to Postings; skips malformed entries."""
    if not isinstance(data, list):
        return []
    out: list[Posting] = []
    for job in data:
        try:
            title = str(job["text"]).strip()
            if not title:
                continue
            cats = job.get("categories") or {}
            location = str(cats.get("location") or "")
            commitment = str(cats.get("commitment") or "")
            workplace = str(job.get("workplaceType") or "").lower()
            if workplace == "remote" or REMOTE_RE.search(location):
                remote: bool | None = True
            elif workplace == "onsite":
                remote = False
            else:
                remote = None
            tags = tuple(str(cats[k]) for k in ("department", "team", "commitment") if cats.get(k))
            is_intern = INTERN_RE.search(commitment) or INTERN_RE.search(title)
            out.append(
                Posting(
                    source="lever",
                    external_id=f"{company_slug}:{job['id']}",
                    title=title,
                    company=company or company_slug,
                    url=str(job["hostedUrl"]),
                    location=location,
                    remote=remote,
                    description=str(job.get("descriptionPlain") or "")
                    or html_to_text(str(job.get("description") or "")),
                    posted_at=_ms_to_dt(job.get("createdAt")),
                    published_at=_ms_to_dt(job.get("createdAt")),
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=tags,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _companies(cfg: AppConfig) -> list[tuple[str, str]]:
    section = cfg.source("lever")
    names = section.get("names") or {}
    return [(str(c), str(names.get(str(c), ""))) for c in section.get("companies") or []]


@register("lever")
class LeverSource:
    name = "lever"
    default_tier = "tier1"  # config watchlist boards (D-186)
    family = "ats"
    description = "Lever job postings for a company watchlist"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("lever") and bool(_companies(cfg))

    def targets(self, ctx: SourceContext) -> list[Target]:
        companies = _companies(ctx.cfg)
        return [Target("lever", s, company=c) for s, c in companies] + discovered_targets(
            ctx, "lever", [s for s, _ in companies]
        )

    async def fetch_target(self, ctx: SourceContext, target: Target) -> list[Posting]:
        slug = target.key
        data = await fetch_board_json(ctx, target, API.format(company=slug), {"mode": "json"})
        if data is None:
            raise SourceUnavailable(f"lever slug {slug}: no data")
        return parse_postings(data, slug, target.company)

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        return await fetch_all_targets(self, ctx)
