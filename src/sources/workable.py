"""Workable public accounts API source (D-173, FC-18)."""

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
    validate_board,
)
from src.text import INTERN_RE, html_to_text

API = "https://www.workable.com/api/accounts/{slug}"
APPLY_HOST = "apply.workable.com"


def _workable_company(data: Any) -> str | None:
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return str(data.get("name") or "")
    return None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_jobs(data: Any, slug: str, company: str) -> list[Posting]:
    """Map a Workable account payload to Postings; skips malformed entries."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    company = company or str(data.get("name") or "") or slug
    out: list[Posting] = []
    for job in data["jobs"]:
        try:
            title = str(job["title"]).strip()
            code = str(job.get("shortcode") or job.get("code") or "").strip()
            url = str(job.get("url") or job.get("shortlink") or "").strip()
            if not title or not code or not url:
                continue
            host = url.split("/")[2] if url.startswith("https://") else ""
            if host != APPLY_HOST:
                continue
            remote = True if job.get("telecommuting") is True else None
            parts = [str(job.get(k) or "").strip() for k in ("city", "country")]
            location = "Remote" if remote else ", ".join(p for p in parts if p)
            etype = str(job.get("employment_type") or "")
            is_intern = INTERN_RE.search(etype) or INTERN_RE.search(title)
            published = _dt(job.get("published_on")) or _dt(job.get("created_at"))
            dept = str(job.get("department") or "")
            out.append(
                Posting(
                    source="workable",
                    external_id=f"{slug}:{code}",
                    title=title,
                    company=company,
                    url=url,
                    location=location,
                    remote=remote,
                    description=html_to_text(str(job.get("description") or "")),
                    posted_at=published,
                    published_at=published,
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                    raw_tags=(dept,) if dept else (),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            continue
    return out


def _accounts(cfg: AppConfig) -> list[tuple[str, str]]:
    section = cfg.source("workable")
    names = section.get("names") or {}
    return [(str(a), str(names.get(str(a), ""))) for a in section.get("accounts") or []]


@register("workable")
class WorkableSource:
    name = "workable"
    default_tier = "tier1"  # config watchlist boards (D-186)
    family = "ats"
    description = "Workable job postings for an account watchlist"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        if not cfg.source_enabled("workable"):
            return False
        if _accounts(cfg):
            return True
        if not cfg.discovery.enabled:
            return False
        from src.discovery import store

        return bool(store.active_boards(cfg.discovery.boards_file, "workable"))

    def targets(self, ctx: SourceContext) -> list[Target]:
        accounts = _accounts(ctx.cfg)
        return [Target("workable", s, company=c) for s, c in accounts] + discovered_targets(
            ctx, "workable", [s for s, _ in accounts]
        )

    async def fetch_target(self, ctx: SourceContext, target: Target) -> list[Posting]:
        slug = target.key
        data = await fetch_board_json(ctx, target, API.format(slug=slug), {"details": "true"})
        if data is None:
            raise SourceUnavailable(f"workable account {slug}: no data")
        return parse_jobs(data, slug, target.company)

    async def validate(self, ctx: SourceContext, token: str) -> tuple[bool, str]:
        """Is `token` a real account? One request to the canonical API URL (FC-16)."""
        return await validate_board(ctx, API.format(slug=token), None, _workable_company)

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        return await fetch_all_targets(self, ctx)
