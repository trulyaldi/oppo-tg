"""hh.kz source via the HeadHunter API at api.hh.ru (D-31, D-47)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import RequestTally, SourceContext, register
from src.text import html_to_text

API = "https://api.hh.ru/vacancies"
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)
CACHE_TTL_S = 3600.0
NEEDS_TOKEN = "needs HH_ACCESS_TOKEN"


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _snippet(item: dict[str, Any]) -> str:
    snip = item.get("snippet") or {}
    parts = [html_to_text(str(snip.get(k) or "")) for k in ("requirement", "responsibility")]
    return "\n".join(p for p in parts if p)


def parse_vacancies(data: Any) -> list[Posting]:
    """Map an api.hh.ru /vacancies payload to Postings; skips malformed entries."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    out: list[Posting] = []
    for item in data["items"]:
        try:
            title = str(item["name"]).strip()
            if not title:
                continue
            area = item.get("area") or {}
            employer = item.get("employer") or {}
            schedule = (item.get("schedule") or {}).get("id")
            experience = (item.get("experience") or {}).get("id")
            is_intern = experience == "noExperience" or bool(INTERN_RE.search(title))
            out.append(
                Posting(
                    source="hh",
                    external_id=str(item["id"]),
                    title=title,
                    company=str(employer.get("name") or ""),
                    url=str(item["alternate_url"]),
                    location=str(area.get("name") or ""),
                    remote=True if schedule == "remote" else (False if schedule else None),
                    description=_snippet(item),
                    posted_at=_dt(item.get("published_at")),
                    kind=Kind.INTERNSHIP if is_intern else Kind.JOB,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _queries(cfg: AppConfig) -> list[str]:
    return [str(q) for q in cfg.source("hh").get("queries") or [] if str(q).strip()]


@register("hh")
class HHSource:
    name = "hh"
    credentials = ("HH_ACCESS_TOKEN",)
    description = "hh.kz vacancies (HeadHunter API, needs HH_ACCESS_TOKEN)"

    def disabled_reason(self, settings: Settings, cfg: AppConfig) -> str | None:
        """Why the source is off (shown by /sources), or None when it is on."""
        if not cfg.source_enabled("hh"):
            return "disabled in config"
        if not settings.hh_access_token:
            return NEEDS_TOKEN  # D-07: api.hh.ru refuses anonymous calls with 403
        if not _queries(cfg):
            return "no queries configured"
        return None

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return self.disabled_reason(settings, cfg) is None

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        cfg = ctx.cfg.source("hh")
        headers = {"HH-User-Agent": ctx.http.user_agent}
        if ctx.settings.hh_access_token:
            headers["Authorization"] = f"Bearer {ctx.settings.hh_access_token}"
        tally = RequestTally()
        seen: dict[str, Posting] = {}
        for query in _queries(ctx.cfg):
            try:
                data = await ctx.http.get_json(
                    API,
                    params={
                        "area": int(cfg.get("area", 40)),
                        "text": query,
                        "per_page": 50,
                        "period": int(cfg.get("period", 14)),
                    },
                    headers=headers,
                    ttl_s=CACHE_TTL_S,
                )
                if data is None:
                    ctx.log.warning("hh query %r: no data", query)
                    tally.failure()
                    continue
                tally.success()
                for p in parse_vacancies(data):
                    seen.setdefault(p.external_id, p)
            except Exception:  # noqa: BLE001 - one bad query must not stop the others
                ctx.log.exception("hh query %r failed", query)
                tally.failure()
        tally.raise_if_all_failed()
        return list(seen.values())
