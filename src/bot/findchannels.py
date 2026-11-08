"""/findchannels and the tc: button callbacks (KF-23). Chat checks: WhitelistMiddleware."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from src.bot.handlers import _reload_listener, _say
from src.clock import Clock
from src.config import AppConfig, Settings
from src.discovery import telegram_flow as flow
from src.pipeline.filters import load_filter_cues


async def cmd_findchannels(
    message: Any,
    bot: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    args: str | None = None,
    factory: Callable[[Settings], Any] | None = None,
) -> None:
    queries = flow.parse_keywords(args)
    if isinstance(queries, str):
        await _say(message, queries)
        return
    candidates, note = await flow.run_discovery(
        bot, message.chat.id, conn, settings, cfg, clock, load_filter_cues(cfg), queries, factory
    )
    for c in candidates:
        await _say(
            message,
            flow.render_candidate(c),
            reply_markup=flow.candidate_keyboard(c.handle),
            disable_web_page_preview=True,
        )
    if note:
        await _say(message, flow.esc(note))
    elif not candidates:
        await _say(message, flow.NO_NEW_TEXT)


async def on_candidate_callback(
    cb: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig
) -> None:
    await flow.on_candidate_callback(cb, conn, clock, cfg, _reload_listener)
