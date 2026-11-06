"""Ashby public job-board API source (D-31, D-43)."""

from __future__ import annotations

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

API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
CACHE_TTL_S = 3600.0


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_board(data: Any, board: str, company: str) -> list[Posting]:
    """Map an Ashby job-board payload to Postings; skips malformed and unlisted entries."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    out: list[Posting] = []
    for job in data["jobs"]:
        try:
            if job.get("isListed") is False:
                continue
            title = str(job["title"]).strip()
            if not title:
                continue
            is_remote = job.get("isRemote")
            remote = is_remote if isinstance(is_remote, bool) else None
            emp = str(job.get("employmentType") or "")
            tags = tuple(
                str(job[k]) for k in ("department", "team", "employmentType") if job.get(k)
            )
            is_intern = INTERN_RE.search(emp) or INTERN_RE.search(title)
            out.append(
                Posting(
                    source="ashby",
                    external_id=f"{board}:{job['id']}",
                    title=title,
                    company=company or board,
                    url=str(job["jobUrl"]),
                    location=str(job.get("location") or ""),
                    remote=remote,
                    description=str(job.get("descriptionPlain") or "")
                    or html_to_text(str(job.get("descriptionHtml") or "")),
                    posted_at=_parse_dt(job.get("publishedAt")),
                    published_at=_parse_dt(job.get("publishedAt")),
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=tags,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _boards(cfg: AppConfig) -> list[tuple[str, str]]:
    section = cfg.source("ashby")
    names = section.get("names") or {}
    return [(str(b), str(names.get(str(b), ""))) for b in section.get("boards") or []]


@register("ashby")
class AshbySource:
    name = "ashby"
    default_tier = "tier1"  # config watchlist boards (D-186)
    family = "ats"
    description = "Ashby job boards for a company watchlist"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("ashby") and bool(_boards(cfg))

    def targets(self, ctx: SourceContext) -> list[Target]:
        boards = _boards(ctx.cfg)
        return [Target("ashby", b, company=c) for b, c in boards] + discovered_targets(
            ctx, "ashby", [b for b, _ in boards]
        )

    async def fetch_target(self, ctx: SourceContext, target: Target) -> list[Posting]:
        board = target.key
        data = await fetch_board_json(
            ctx, target, API.format(board=board), {"includeCompensation": "false"}
        )
        if data is None:
            raise SourceUnavailable(f"ashby board {board}: no data")
        return parse_board(data, board, target.company)

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        return await fetch_all_targets(self, ctx)
