"""Live Telethon listener for Kazakh Telegram channels (FC-24, D-193, D-218).

Subscribes `events.NewMessage` to the configured channels the account has already joined,
stores each message at once (never waiting for the pipeline lock) and hands a debounced
batch to `on_new`, which runs dedupe, filter, score and alerts under PIPELINE_LOCK.
Channels that are not joined, and every channel while the listener is disconnected, stay on
tier2 polling. The client is injected (D-05); tests use a fake client and fake events.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from src import db as dbm
from src.config import AppConfig, Settings
from src.models import Posting
from src.pipeline.dedupe import fingerprint
from src.sources.telegram_channels import (
    NEEDS_CREDS,
    NO_CHANNELS,
    NO_SESSION,
    advance_last_id,
    channels,
    default_client_factory,
    parse_message,
    session_exists,
)
from src.sources.telegram_ro import read_only

log = logging.getLogger(__name__)

DEFAULT_FLUSH_S = 30.0

ClientFactory = Callable[[Settings], Any]
OnNew = Callable[[list[Posting]], Awaitable[object]]


class TelegramListener:
    def __init__(
        self,
        settings: Settings,
        cfg: AppConfig,
        conn: sqlite3.Connection,
        client_factory: ClientFactory | None = None,
        on_new: OnNew | None = None,
        *,
        clock: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        require_session_file: bool = True,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self.conn = conn
        self._factory = client_factory or default_client_factory
        self._on_new = on_new
        self._clock = clock
        self._sleep = sleep
        self._require_session = require_session_file
        self.client: Any = None
        self.started = False
        self.reason: str | None = None
        self._live: set[str] = set()
        self._queue: list[Posting] = []
        self._flush_task: asyncio.Task | None = None
        self._handler_added = False

    # ----- config -----

    @property
    def flush_s(self) -> float:
        try:
            return float(self.cfg.source("telegram_channels").get("live_flush_s", DEFAULT_FLUSH_S))
        except (TypeError, ValueError):
            return DEFAULT_FLUSH_S

    # ----- state -----

    def live_channels(self) -> set[str]:
        """Channels delivered live right now; empty while the client is disconnected."""
        if not self.started or self.client is None:
            return set()
        try:
            if not self.client.is_connected():
                return set()
        except Exception:  # noqa: BLE001
            return set()
        return set(self._live)

    def _now(self) -> Any:
        from src.clock import SystemClock

        return (self._clock or SystemClock()).now()

    # ----- lifecycle -----

    def _blocked_reason(self) -> str | None:
        if not self.cfg.source_enabled("telegram_channels"):
            return "disabled in config"
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            return NEEDS_CREDS
        if self._require_session and not session_exists(self.settings):
            return NO_SESSION
        if not channels(self.cfg, self.conn):
            return NO_CHANNELS
        return None

    async def start(self) -> bool:
        """Connect and subscribe. False (logged once, never raising) when it cannot run."""
        reason = self._blocked_reason()
        if reason is not None:
            self._fail(f"live listener not started: {reason}; polling only")
            return False
        client = None
        try:
            client = read_only(self._factory(self.settings))
            await client.connect()
            if not await client.is_user_authorized():
                await self._discard(client)
                self._fail("live listener not started: Telethon session not authorized")
                return False
            self.client = client
            self.started = True
            await self._subscribe()
        except Exception as exc:  # noqa: BLE001 - the bot must run without the listener
            log.exception("live listener failed to start")
            await self._discard(client)
            self.client = None
            self.started = False
            self.reason = f"error: {type(exc).__name__}"
            return False
        self.reason = None
        log.info("live listener started on %d channels", len(self._live))
        return True

    def _fail(self, message: str) -> None:
        self.reason = message
        log.warning(message)

    async def _discard(self, client: Any) -> None:
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            log.debug("live listener: disconnect failed", exc_info=True)

    async def _joined(self) -> set[str]:
        """Lower-cased usernames of the channels the account belongs to."""
        joined: set[str] = set()
        async for dialog in self.client.iter_dialogs():
            name = getattr(getattr(dialog, "entity", None), "username", None)
            if isinstance(name, str) and name:
                joined.add(name.casefold())
        return joined

    async def _subscribe(self) -> None:
        from telethon import events

        if self._handler_added:
            self.client.remove_event_handler(self._on_event)
            self._handler_added = False
        wanted = channels(self.cfg, self.conn)
        joined = await self._joined()
        self._live = {c for c in wanted if c.casefold() in joined}
        skipped = [c for c in wanted if c not in self._live]
        if skipped:
            log.info("not joined, kept on polling: %s", ", ".join(skipped))
        self._record_state(self._live)
        if self._live:
            self.client.add_event_handler(
                self._on_event, events.NewMessage(chats=sorted(self._live))
            )
            self._handler_added = True

    def _record_state(self, handles: set[str]) -> None:
        """Live channels get a baselined target_state row, so a later poll is not a baseline.

        Without it, messages missed while the listener was down would be treated as a
        first fetch and never alert (D-270).
        """
        from src import db

        now = self._now()
        for handle in sorted(handles):
            db.record_target_attempt(self.conn, "telegram_channels", handle, now, ok=True)
            db.mark_baselined(self.conn, "telegram_channels", handle, now)

    async def reload(self) -> None:
        """Re-read the channel list (after a /channels change); no-op when not running."""
        if not self.started or self.client is None:
            return
        try:
            await self._subscribe()
        except Exception:  # noqa: BLE001
            log.exception("live listener reload failed; keeping the previous subscription")

    async def stop(self) -> None:
        task = self._flush_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._flush_task = None
        client, self.client, self.started = self.client, None, False
        self._live = set()
        await self._discard(client)

    # ----- events -----

    async def _channel_of(self, event: Any) -> str | None:
        chat = getattr(event, "chat", None)
        if chat is None and hasattr(event, "get_chat"):
            try:
                chat = await event.get_chat()
            except Exception:  # noqa: BLE001
                chat = None
        name = getattr(chat, "username", None)
        if not isinstance(name, str):
            return None
        return next((c for c in self._live if c.casefold() == name.casefold()), None)

    async def _on_event(self, event: Any) -> None:
        """Store one new message, then advance last_id (after the store). Never raises."""
        try:
            channel = await self._channel_of(event)
            msg = getattr(event, "message", None)
            if channel is None or msg is None:
                return
            mid = int(msg.id)
            posting = parse_message(channel, msg)
            if posting is not None:
                dbm.upsert_posting(
                    self.conn,
                    posting,
                    fingerprint(posting),
                    self._now(),
                    target=channel,
                    baseline=False,
                )
            advance_last_id(self.conn, channel, mid)  # only after the store
            if posting is not None:
                self._enqueue(posting)
        except Exception:  # noqa: BLE001 - one bad event must not kill the listener
            log.exception("live listener: event failed")

    def _enqueue(self, posting: Posting) -> None:
        self._queue.append(posting)
        if self._on_new is None:
            self._queue.clear()
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.ensure_future(self._flush())

    async def _flush(self) -> None:
        """Debounce: wait `live_flush_s`, then hand the whole batch over. Lock-free storing."""
        while self._queue:
            await self._sleep(self.flush_s)
            batch, self._queue = self._queue, []
            if not batch or self._on_new is None:
                continue
            try:
                await self._on_new(batch)
            except Exception:  # noqa: BLE001
                log.exception("live listener: pipeline flush failed")
