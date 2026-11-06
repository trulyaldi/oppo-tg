"""Kazakh Telegram job channels via Telethon (credential-gated, D-69).

Read-only, public channels listed in config.toml only. The Telethon client is injected
(D-05); the default factory imports telethon lazily and reuses an existing session file.
Message text is untrusted data: it is only stored as plain text, never executed.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import SourceContext, register

BATCH = 100
FLOOD_SLEEP_THRESHOLD_S = 60
CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
COMPANY_RE = re.compile(
    r"^\s*(?:company|компания)\s*[:：]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b|стажиров|стажёр|стажер", re.IGNORECASE)
AD_KEYWORDS = (
    "#реклама",
    "на правах рекламы",
    "рекламный пост",
    "#ad",
    "sponsored",
    "advertisement",
    "промокод",
)
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
NEEDS_CREDS = "needs TG_API_ID, TG_API_HASH"
NO_SESSION = "no Telethon session (run python -m src.tools.telethon_login)"
NO_CHANNELS = "no channels configured"

ClientFactory = Callable[[Settings], Any]


def normalize_channel(raw: Any) -> str | None:
    """'@name', 't.me/name' or 'https://t.me/name' -> 'name'; None if not a public handle."""
    name = str(raw or "").strip()
    name = re.sub(r"^(?:https?://)?(?:t\.me/|telegram\.me/)", "", name).lstrip("@").rstrip("/")
    return name if CHANNEL_RE.match(name) else None


def channels(cfg: AppConfig) -> list[str]:
    out: list[str] = []
    for raw in cfg.source("telegram_channels").get("channels") or []:
        name = normalize_channel(raw)
        if name and name not in out:
            out.append(name)
    return out


def session_exists(settings: Settings) -> bool:
    p = Path(settings.tg_session_path)
    return p.is_file() or Path(str(p) + ".session").is_file()


def is_ad(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in AD_KEYWORDS)


def parse_message(channel: str, msg: Any) -> Posting | None:
    """One Telethon message -> Posting; None for empty, ad or malformed messages."""
    try:
        text = getattr(msg, "message", None)
        msg_id = int(msg.id)
        if not isinstance(text, str):
            return None
        text = CTRL_RE.sub("", text).strip()
        if not text or is_ad(text):
            return None
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not first:
            return None
        date = getattr(msg, "date", None)
        if isinstance(date, datetime):
            date = date if date.tzinfo else date.replace(tzinfo=UTC)
        else:
            date = None
        m = COMPANY_RE.search(text)
        return Posting(
            source="telegram_channels",
            external_id=f"{channel}:{msg_id}",
            title=first[:120],
            company=m.group(1)[:120] if m else "",
            url=f"https://t.me/{channel}/{msg_id}",
            description=text,
            posted_at=date,
            kind=Kind.INTERNSHIP if INTERN_RE.search(text[:300]) else Kind.JOB,
            raw_tags=(channel,),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def default_client_factory(settings: Settings) -> Any:
    """Real Telethon client on the existing session. Never used in tests."""
    from telethon import TelegramClient  # lazy: only needed for live runs

    Path(settings.tg_session_path).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        settings.tg_session_path,
        int(settings.tg_api_id or 0),
        settings.tg_api_hash or "",
        flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD_S,
    )


def _state_key(channel: str) -> str:
    return f"telegram_channels:last_id:{channel}"


@register("telegram_channels")
class TelegramChannelsSource:
    name = "telegram_channels"
    credentials = ("TG_API_ID", "TG_API_HASH")
    description = "Kazakh Telegram job channels (Telethon, needs TG_API_ID/TG_API_HASH + session)"

    def __init__(
        self,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pause_s: float = 1.0,
        require_session_file: bool = True,
    ) -> None:
        self._factory = client_factory or default_client_factory
        self._sleep = sleep
        self._pause_s = pause_s
        self._require_session = require_session_file

    def disabled_reason(self, settings: Settings, cfg: AppConfig) -> str | None:
        if not cfg.source_enabled(self.name):
            return "disabled in config"
        if not settings.tg_api_id or not settings.tg_api_hash:
            return NEEDS_CREDS
        if self._require_session and not session_exists(settings):
            return NO_SESSION  # D-07
        if not channels(cfg):
            return NO_CHANNELS
        return None

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return self.disabled_reason(settings, cfg) is None

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        if not self.enabled(ctx.settings, ctx.cfg):
            return []
        from telethon.errors import FloodWaitError

        db = ctx.http.db
        out: list[Posting] = []
        client = None
        try:
            client = self._factory(ctx.settings)
            await client.connect()
            if not await client.is_user_authorized():
                ctx.log.warning("telegram_channels: session not authorized, skipping")
                return []
            for i, channel in enumerate(channels(ctx.cfg)):
                if i and self._pause_s:
                    await self._sleep(self._pause_s)
                try:
                    out.extend(await self._read_channel(client, channel, ctx, db))
                except FloodWaitError as exc:
                    ctx.log.warning(
                        "telegram_channels: flood wait %ss on %s, stopping run",
                        getattr(exc, "seconds", "?"),
                        channel,
                    )
                    break
                except Exception:  # noqa: BLE001 - one bad channel must not stop the others
                    ctx.log.exception("telegram_channels: channel %s failed", channel)
            return out
        except Exception:  # noqa: BLE001 - a failing source must not crash the pipeline
            ctx.log.exception("telegram_channels failed")
            return out
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    ctx.log.debug("telegram_channels: disconnect failed", exc_info=True)

    async def _read_channel(
        self, client: Any, channel: str, ctx: SourceContext, db: Any
    ) -> list[Posting]:
        from src import db as dbm

        last = int(dbm.get_state(db, _state_key(channel), "0") or 0)
        posts: list[Posting] = []
        max_id = last
        async for msg in client.iter_messages(channel, limit=BATCH, min_id=last):
            try:
                mid = int(msg.id)
            except (TypeError, ValueError, AttributeError):
                continue
            if mid <= last:
                continue
            max_id = max(max_id, mid)
            p = parse_message(channel, msg)
            if p is not None:
                posts.append(p)
        if max_id > last:  # only after a fully successful read of this channel
            dbm.set_state(db, _state_key(channel), str(max_id))
        return posts
