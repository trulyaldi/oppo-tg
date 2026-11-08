"""Kazakh Telegram job channels via Telethon (credential-gated, D-69).

Read-only, public channels listed in config.toml only. The Telethon client is injected
(D-05); the default factory imports telethon lazily and reuses an existing session file.
Message text is untrusted data: it is only stored as plain text, never executed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import (
    RequestTally,
    SourceContext,
    SourceUnavailable,
    Target,
    TargetResult,
    register,
    run_target,
)
from src.sources.telegram_ro import ReadOnlyViolation, read_only

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


MAX_CHANNELS = 50
DEFAULT_MAX_CHANNELS_PER_POLL = 20
DEFAULT_MAX_RESOLVES_PER_POLL = 3
DEFAULT_PAUSE_S = 4.0  # D-252: 20 channels in about 80 s, FloodWait-safe
RERESOLVE_AFTER = timedelta(days=1)
FLOOD_KEY = "tg_flood_until"
FLOOD_LOGGED_KEY = "tg_flood_logged"


def poll_option(cfg: AppConfig, key: str, default: Any) -> Any:
    return cfg.source("telegram_channels").get(key, default)


def flood_until(conn: Any) -> datetime | None:
    """When the current Telegram flood wait ends (None when there is none)."""
    from src import db as dbm

    raw = dbm.get_state(conn, FLOOD_KEY, "")
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def flood_remaining(conn: Any, now: datetime) -> int:
    """Whole seconds left of the flood wait, 0 when clear."""
    until = flood_until(conn)
    if until is None or now >= until:
        return 0
    return int((until - now).total_seconds() + 0.999)


def set_flood(conn: Any, now: datetime, seconds: Any) -> datetime:
    """now + ceil(seconds x 1.1), kept if a longer wait is already recorded (KF-19)."""
    from src import db as dbm

    try:
        secs = max(int(seconds), 0)
    except (TypeError, ValueError):
        secs = 60
    until = now + timedelta(seconds=-(-secs * 11 // 10))  # integer ceil(secs * 1.1)
    old = flood_until(conn)
    if old is not None and old > until:
        return old
    dbm.set_state(conn, FLOOD_KEY, until.isoformat())
    return until


def flood_gate(conn: Any, now: datetime, log: logging.Logger, who: str) -> int:
    """Seconds of flood wait left; logs once per flood episode. Callers make no request if > 0."""
    from src import db as dbm

    rem = flood_remaining(conn, now)
    if rem:
        until = flood_until(conn)
        stamp = until.isoformat() if until else ""
        if dbm.get_state(conn, FLOOD_LOGGED_KEY, "") != stamp:
            dbm.set_state(conn, FLOOD_LOGGED_KEY, stamp)
            log.warning("%s: telegram flood wait, no requests for %ss", who, rem)
    return rem


def config_channels(cfg: AppConfig) -> list[str]:
    out: list[str] = []
    for raw in cfg.source("telegram_channels").get("channels") or []:
        name = normalize_channel(raw)
        if name and name not in out:
            out.append(name)
    return out


def channels(cfg: AppConfig, conn: Any = None) -> list[str]:
    """(config + runtime added) - runtime removed, normalized, deduped, at most 50 (D-193)."""
    out = config_channels(cfg)
    if conn is not None:
        from src import db as dbm

        overlay = dbm.tg_channel_overlay(conn)
        for handle, state in overlay.items():
            name = normalize_channel(handle)
            if state == "added" and name and name not in out:
                out.append(name)
        out = [c for c in out if overlay.get(c) != "removed"]
    return out[:MAX_CHANNELS]


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
            published_at=date,
            kind=Kind.INTERNSHIP if INTERN_RE.search(text[:300]) else Kind.JOB,
            raw_tags=(channel,),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def default_client_factory(settings: Settings) -> Any:
    """Real Telethon client on the existing session. Never used in tests."""
    from telethon import TelegramClient  # lazy: only needed for live runs

    Path(settings.tg_session_path).parent.mkdir(parents=True, exist_ok=True)
    return read_only(
        TelegramClient(
            settings.tg_session_path,
            int(settings.tg_api_id or 0),
            settings.tg_api_hash or "",
            flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD_S,
        )
    )


def _state_key(channel: str) -> str:
    return f"telegram_channels:last_id:{channel}"


def advance_last_id(conn: Any, channel: str, msg_id: int) -> None:
    """Move the channel cursor forward only (never back); call after the store (D-193)."""
    from src import db as dbm

    if msg_id > int(dbm.get_state(conn, _state_key(channel), "0") or 0):
        dbm.set_state(conn, _state_key(channel), str(msg_id))


_LISTENER: Any = None  # the running TelegramListener (run mode), set by main._serve


def set_listener(listener: Any) -> None:
    global _LISTENER
    _LISTENER = listener


def get_listener() -> Any:
    return _LISTENER


def live_channels() -> set[str]:
    """Channels currently delivered by the live listener (empty while disconnected)."""
    return set(_LISTENER.live_channels()) if _LISTENER is not None else set()


@register("telegram_channels")
class TelegramChannelsSource:
    name = "telegram_channels"
    default_tier = "tg"  # D-252: own 4 min loop
    credentials = ("TG_API_ID", "TG_API_HASH")
    description = "Kazakh Telegram job channels (Telethon, needs TG_API_ID/TG_API_HASH + session)"

    def __init__(
        self,
        client_factory: ClientFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pause_s: float | None = None,  # None: [sources.telegram_channels] pause_s
        require_session_file: bool = True,
    ) -> None:
        self._factory = client_factory or default_client_factory
        self._sleep = sleep
        self._pause_s = pause_s
        self._require_session = require_session_file
        self._run_client: Any = None  # set by run_targets: one client for the whole run
        self._flood_seconds: Any = None
        self._resolves_left: int | None = None  # None: unlimited (standalone fetch)

    def _pause(self, cfg: AppConfig) -> float:
        if self._pause_s is not None:
            return self._pause_s
        return float(poll_option(cfg, "pause_s", DEFAULT_PAUSE_S))

    def disabled_reason(self, settings: Settings, cfg: AppConfig, conn: Any = None) -> str | None:
        if not cfg.source_enabled(self.name):
            return "disabled in config"
        if not settings.tg_api_id or not settings.tg_api_hash:
            return NEEDS_CREDS
        if self._require_session and not session_exists(settings):
            return NO_SESSION  # D-07
        if not channels(cfg, conn):
            return NO_CHANNELS
        return None

    def enabled(self, settings: Settings, cfg: AppConfig, conn: Any = None) -> bool:
        return self.disabled_reason(settings, cfg, conn) is None

    def _shared_client(self) -> Any:
        """The live listener's client, so one session file has one client (D-193)."""
        client = getattr(_LISTENER, "client", None) if _LISTENER is not None else None
        return client if client is not None and getattr(_LISTENER, "started", False) else None

    async def _open(self, ctx: SourceContext) -> tuple[Any, bool]:
        """(connected, authorized client, owned). Shared clients are never disconnected."""
        shared = self._shared_client()
        if shared is not None:
            if not shared.is_connected():
                await shared.connect()
            return shared, False
        client = read_only(self._factory(ctx.settings))
        await client.connect()
        return client, True

    async def _close(self, client: Any, owned: bool, ctx: SourceContext) -> None:
        if client is None or not owned:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            ctx.log.debug("telegram_channels: disconnect failed", exc_info=True)

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        """All channels in one run. Raises SourceUnavailable when no channel was read (D-172)."""
        if not self.enabled(ctx.settings, ctx.cfg, ctx.http.db):
            return []
        from telethon.errors import FloodWaitError

        db = ctx.http.db
        if flood_gate(db, ctx.clock.now(), ctx.log, self.name):
            return []
        out: list[Posting] = []
        tally = RequestTally()
        client, owned = None, True
        try:
            client, owned = await self._open(ctx)
            if not await client.is_user_authorized():
                raise SourceUnavailable("telegram session not authorized")
            for i, channel in enumerate(channels(ctx.cfg, db)):
                if i and (pause := self._pause(ctx.cfg)):
                    await self._sleep(pause)
                try:
                    out.extend(await self._read_channel(client, channel, ctx, db))
                    tally.success()
                except FloodWaitError as exc:
                    set_flood(db, ctx.clock.now(), getattr(exc, "seconds", 60))
                    ctx.log.warning(
                        "telegram_channels: flood wait %ss on %s, stopping run",
                        getattr(exc, "seconds", "?"),
                        channel,
                    )
                    tally.failure()
                    break
                except Exception:  # noqa: BLE001 - one bad channel must not stop the others
                    ctx.log.exception("telegram_channels: channel %s failed", channel)
                    tally.failure()
            tally.raise_if_all_failed()
            return out
        finally:
            await self._close(client, owned, ctx)

    def targets(self, ctx: SourceContext) -> list[Target]:
        if not self.enabled(ctx.settings, ctx.cfg, ctx.http.db):
            return []
        live = {c.casefold() for c in live_channels()}
        return [
            Target("telegram_channels", c)
            for c in channels(ctx.cfg, ctx.http.db)
            if c.casefold() not in live  # live channels need no polling (D-193)
        ]

    def select_targets(self, ctx: SourceContext, targets: list[Target]) -> list[Target]:
        """Which channels this run reads (D-252, KF-19).

        Private and invalid channels are skipped; the rest are ordered by `last_polled_at`
        (never polled first) and cut at `max_channels_per_poll`. Channels that still need a
        username lookup are admitted only while the per-run resolve budget lasts.
        """
        from src import db as dbm

        ents = dbm.tg_entities(ctx.http.db)
        cap = int(poll_option(ctx.cfg, "max_channels_per_poll", DEFAULT_MAX_CHANNELS_PER_POLL))
        resolves = int(poll_option(ctx.cfg, "max_resolves_per_poll", DEFAULT_MAX_RESOLVES_PER_POLL))
        live = [(i, t) for i, t in enumerate(targets) if not _blocked(ents.get(t.key.casefold()))]
        live.sort(key=lambda it: (_last_polled(ents.get(it[1].key.casefold())), it[0]))
        picked: list[Target] = []
        for _, t in live:
            if len(picked) >= cap:
                break
            if not _resolved(ents.get(t.key.casefold())):
                if resolves <= 0:
                    continue
                resolves -= 1
            picked.append(t)
        return picked

    async def run_targets(self, ctx: SourceContext, targets: list[Target]) -> list[TargetResult]:
        """Poll the due channels with one client, paused between channels (FIX-40, KF-19).

        During a flood wait nothing is requested. A new flood wait stops the run: the remaining
        channels fail without any request and keep their cursors. Never raises.
        """
        results: list[TargetResult] = []
        if flood_gate(ctx.http.db, ctx.clock.now(), ctx.log, self.name):
            return results
        targets = self.select_targets(ctx, targets)
        if not targets:
            return results
        client, owned = None, True
        try:
            try:
                client, owned = await self._open(ctx)
                if not await client.is_user_authorized():
                    raise SourceUnavailable("telegram session not authorized")
            except Exception as exc:  # noqa: BLE001 - isolation boundary
                ctx.log.exception("telegram_channels: cannot open client")
                err = f"{type(exc).__name__}: {exc}"
                return [TargetResult(self.name, t, False, [], err) for t in targets]
            self._run_client, self._flood_seconds = client, None
            self._resolves_left = int(
                poll_option(ctx.cfg, "max_resolves_per_poll", DEFAULT_MAX_RESOLVES_PER_POLL)
            )
            for i, target in enumerate(targets):
                if self._flood_seconds is not None:
                    results.append(
                        TargetResult(
                            self.name,
                            target,
                            False,
                            [],
                            f"skipped: telegram flood wait {self._flood_seconds}s",
                        )
                    )
                    continue
                if i and (pause := self._pause(ctx.cfg)):
                    await self._sleep(pause)
                results.append(await run_target(self, ctx, target))
                if self._flood_seconds is not None:
                    ctx.log.warning(
                        "telegram_channels: flood wait %ss on %s, stopping run",
                        self._flood_seconds,
                        target.key,
                    )
            return results
        finally:
            self._run_client = None
            self._resolves_left = None
            await self._close(client, owned, ctx)

    async def fetch_target(self, ctx: SourceContext, target: Target) -> list[Posting]:
        """One channel per call; raises on any failure so the target is marked failed.

        Inside `run_targets` the run's client is reused; standalone it opens its own.
        `last_id` is advanced by a post-store hook, not here (D-193).
        """
        from telethon.errors import FloodWaitError

        from src import db as dbm

        db = ctx.http.db
        if self._run_client is None and flood_gate(db, ctx.clock.now(), ctx.log, self.name):
            raise SourceUnavailable("telegram flood wait")
        client, owned = self._run_client, False
        try:
            if client is None:
                client, owned = await self._open(ctx)
                if not await client.is_user_authorized():
                    raise SourceUnavailable("telegram session not authorized")
            return await self._read_channel(client, target.key, ctx, db, commit=False)
        except FloodWaitError as exc:
            self._flood_seconds = getattr(exc, "seconds", "?")
            set_flood(db, ctx.clock.now(), getattr(exc, "seconds", 60))
            raise
        except Exception:
            # a failing channel must not hog the front of the queue
            dbm.touch_tg_polled(db, target.key, ctx.clock.now())
            raise
        finally:
            if owned:
                await self._close(client, owned, ctx)

    async def _resolve(self, client: Any, channel: str, ctx: SourceContext) -> Any:
        """get_input_entity once, cache id, hash and status. None: private or invalid.

        Only a public channel or supergroup is ever stored as `ok` (the wrapper refuses users
        and basic groups, and the class is checked again here).
        """
        from telethon.errors import (
            ChannelPrivateError,
            UsernameInvalidError,
            UsernameNotOccupiedError,
        )

        from src import db as dbm

        db, now = ctx.http.db, ctx.clock.now()
        if self._resolves_left is not None:
            if self._resolves_left <= 0:
                raise SourceUnavailable("resolve budget used up for this run")
            self._resolves_left -= 1
        try:
            peer = await client.get_input_entity(channel)
        except ChannelPrivateError:
            dbm.set_tg_entity(db, channel, "private", now)
            return None
        except (UsernameInvalidError, UsernameNotOccupiedError, ReadOnlyViolation):
            dbm.set_tg_entity(db, channel, "invalid", now)
            return None
        if type(peer).__name__ != "InputPeerChannel":
            dbm.set_tg_entity(db, channel, "invalid", now)
            return None
        dbm.set_tg_entity(
            db,
            channel,
            "ok",
            now,
            channel_id=int(peer.channel_id),
            access_hash=int(peer.access_hash),
        )
        return peer

    async def _peer(self, client: Any, channel: str, ctx: SourceContext) -> Any:
        """The cached InputPeerChannel, resolving the handle only when it is missing."""
        from telethon.tl.types import InputPeerChannel

        from src import db as dbm

        ent = dbm.get_tg_entity(ctx.http.db, channel)
        if ent is not None and ent["status"] != "ok":
            return None
        if _resolved(ent):
            return InputPeerChannel(int(ent["channel_id"]), int(ent["access_hash"]))
        return await self._resolve(client, channel, ctx)

    async def _re_resolve(self, client: Any, channel: str, ctx: SourceContext) -> Any:
        """After CHANNEL_INVALID or a peer error: look the handle up again, at most daily."""
        from src import db as dbm

        ent = dbm.get_tg_entity(ctx.http.db, channel)
        stamp = ent["resolved_at"] if ent is not None else None
        if stamp:
            try:
                at = datetime.fromisoformat(stamp)
            except ValueError:
                at = None
            if at is not None:
                at = at if at.tzinfo else at.replace(tzinfo=UTC)
                if ctx.clock.now() - at < RERESOLVE_AFTER:
                    return None
        return await self._resolve(client, channel, ctx)

    async def _read_channel(
        self, client: Any, channel: str, ctx: SourceContext, db: Any, commit: bool = True
    ) -> list[Posting]:
        from telethon.errors import (
            ChannelInvalidError,
            ChannelPrivateError,
            PeerIdInvalidError,
        )

        from src import db as dbm

        now = ctx.clock.now()
        peer = await self._peer(client, channel, ctx)
        if peer is None:  # private or invalid: not polled any more
            dbm.touch_tg_polled(db, channel, now)
            return []
        last = int(dbm.get_state(db, _state_key(channel), "0") or 0)
        posts: list[Posting] = []
        max_id = last
        title: str | None = None
        for attempt in (0, 1):
            posts, max_id, title = [], last, None
            try:
                async for msg in client.iter_messages(peer, limit=BATCH, min_id=last):
                    try:
                        mid = int(msg.id)
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if mid <= last:
                        continue
                    max_id = max(max_id, mid)
                    title = title or _chat_title(msg)
                    p = parse_message(channel, msg)
                    if p is not None:
                        posts.append(p)
                break
            except ChannelPrivateError:
                dbm.set_tg_entity(db, channel, "private", now)
                dbm.touch_tg_polled(db, channel, now)
                return []
            except (ChannelInvalidError, PeerIdInvalidError):
                if attempt:
                    raise
                peer = await self._re_resolve(client, channel, ctx)
                if peer is None:
                    raise
        if title:
            db.execute(
                "UPDATE tg_entities SET title=? WHERE handle=? COLLATE NOCASE", (title, channel)
            )
            db.commit()
        dbm.touch_tg_polled(db, channel, now)
        if max_id > last:
            if commit:  # legacy fetch(): no store step, advance after a full read
                dbm.set_state(db, _state_key(channel), str(max_id))
            else:
                ctx.after_store[("telegram_channels", channel)] = lambda: advance_last_id(
                    db, channel, max_id
                )
        return posts


def _resolved(ent: Any) -> bool:
    return (
        ent is not None
        and ent["status"] == "ok"
        and ent["channel_id"] is not None
        and ent["access_hash"] is not None
    )


def _blocked(ent: Any) -> bool:
    return ent is not None and ent["status"] in ("private", "invalid")


def _last_polled(ent: Any) -> str:
    return (ent["last_polled_at"] or "") if ent is not None else ""


def _chat_title(msg: Any) -> str | None:
    t = getattr(getattr(msg, "chat", None), "title", None)
    return t[:200] if isinstance(t, str) and t else None
