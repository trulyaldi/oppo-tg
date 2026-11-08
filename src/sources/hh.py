"""hh.kz source via the HeadHunter API at api.hh.ru (D-31, D-47)."""

from __future__ import annotations

import html
import json
import logging
import sqlite3
from datetime import datetime
from typing import Any

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.llm.budget import TZ
from src.models import Kind, Posting
from src.sources.base import RequestTally, SourceContext, SourceUnavailable, register
from src.text import INTERN_RE, html_to_text

API = "https://api.hh.ru/vacancies"
CACHE_TTL_S = 300.0  # below the 10 min tier1 cadence, so every run is fresh (D-285)
NEEDS_TOKEN = "needs HH_ACCESS_TOKEN"
PENDING_KEY = "hh_auth_error_pending"
NOTICE_DAY_KEY = "hh_auth_notice_day"
UA_ERROR = (
    "hh rejected the request (HTTP 400): hh requires a User-Agent with app name and "
    "contact email; set HTTP_CONTACT to your email"
)

log = logging.getLogger(__name__)


def token_error(status: int) -> str:
    return (
        f"hh refused the token (HTTP {status}): HH_ACCESS_TOKEN must be an hh application "
        "token (client_credentials) from dev.hh.ru; user tokens expire after 14 days "
        "and are not supported"
    )


def hh_user_agent(contact: str) -> str:
    """The documented `AppName/version (contact)` format (D-182)."""
    return f"oppo-tg/0.1 ({contact})"


def _note_auth_error(ctx: SourceContext, message: str) -> None:
    """Leave the message for the once-a-day Telegram notice (the collector has no bot)."""
    if ctx.http.db is not None:
        db.set_state(ctx.http.db, PENDING_KEY, message)


async def notify_auth_error_once(
    bot: object, chat_id: int | str, conn: sqlite3.Connection, clock: Clock
) -> bool:
    """One Telegram notice per Asia/Almaty day for an hh auth or User-Agent error. Never raises."""
    try:
        message = db.get_state(conn, PENDING_KEY)
        if not message:
            return False
        day = clock.now().astimezone(TZ).date().isoformat()
        if db.get_state(conn, NOTICE_DAY_KEY) == day:
            return False
        text = f"<b>hh source error</b>\n{html.escape(message)}"
        await bot.send_message(chat_id, text, parse_mode="HTML")  # type: ignore[attr-defined]
        db.set_state(conn, NOTICE_DAY_KEY, day)
        db.set_state(conn, PENDING_KEY, "")
        return True
    except Exception:
        log.exception("could not send the hh auth notice")
        return False


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
                    published_at=_dt(item.get("published_at")),
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
    default_tier = "tier1"  # D-252: Kazakhstan source, polled every 10 to 15 min
    min_poll_minutes = 10  # politeness floor: 5 queries per run, at most 30 requests/hour
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
        headers = {"HH-User-Agent": hh_user_agent(ctx.settings.http_contact)}
        if ctx.settings.hh_access_token:
            headers["Authorization"] = f"Bearer {ctx.settings.hh_access_token}"
        tally = RequestTally()
        seen: dict[str, Posting] = {}
        for query in _queries(ctx.cfg):
            try:
                res = await ctx.http.fetch(
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
                if res.status in (400, 401, 403):
                    # Every query would fail the same way: stop and say why (D-182).
                    msg = UA_ERROR if res.status == 400 else token_error(res.status)
                    _note_auth_error(ctx, msg)
                    raise SourceUnavailable(msg)
                try:
                    data = json.loads(res.text) if res.text is not None else None
                except ValueError:
                    data = None
                if data is None:
                    ctx.log.warning("hh query %r: no data", query)
                    tally.failure()
                    continue
                tally.success()
                for p in parse_vacancies(data):
                    seen.setdefault(p.external_id, p)
            except SourceUnavailable:
                raise
            except Exception:  # noqa: BLE001 - one bad query must not stop the others
                ctx.log.exception("hh query %r failed", query)
                tally.failure()
        tally.raise_if_all_failed()
        if tally.partial:
            ctx.note_partial("hh")
        return list(seen.values())
