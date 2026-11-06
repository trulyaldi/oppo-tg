"""Dispatcher construction and single-chat whitelist (SPEC 1.10, D-05)."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Dispatcher, F
from aiogram.types import CallbackQuery, TelegramObject, Update

from src.bot.handlers import handle_feedback
from src.clock import Clock
from src.config import AppConfig, Settings


def update_chat_id(update: Update) -> int | None:
    """Chat id an update originates from, or None if it has no chat."""
    for name in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
    ):
        msg = getattr(update, name, None)
        if msg is not None:
            return msg.chat.id
    cb = update.callback_query
    if cb is not None and cb.message is not None:
        return cb.message.chat.id
    return None


class WhitelistMiddleware(BaseMiddleware):
    """Drop every update that does not come from the one configured chat."""

    def __init__(self, chat_id: str) -> None:
        self.chat_id = str(chat_id).strip()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = update_chat_id(event) if isinstance(event, Update) else None
        if chat is None or str(chat) != self.chat_id:
            return None
        return await handler(event, data)


def build_dispatcher(
    conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, clock: Clock
) -> Dispatcher:
    dp = Dispatcher(conn=conn, settings=settings, cfg=cfg, clock=clock)
    dp.update.outer_middleware(WhitelistMiddleware(settings.telegram_chat_id))

    @dp.callback_query(F.data.startswith("fb:"))
    async def _feedback(cb: CallbackQuery, conn: sqlite3.Connection, clock: Clock) -> None:
        await handle_feedback(cb, conn, clock)

    return dp
