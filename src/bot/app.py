"""Dispatcher construction and single-chat whitelist (SPEC 1.10, D-05)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from src.bot import handlers, outreach_handlers
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
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    client: Any = None,
    http: Any = None,
) -> Dispatcher:
    dp = Dispatcher(conn=conn, settings=settings, cfg=cfg, clock=clock, client=client, http=http)
    dp.update.outer_middleware(WhitelistMiddleware(settings.telegram_chat_id))

    @dp.callback_query(F.data.startswith("fb:"))
    async def _feedback(cb: CallbackQuery, conn: sqlite3.Connection, clock: Clock) -> None:
        await handle_feedback(cb, conn, clock)

    @dp.callback_query(F.data.startswith("pr:"))
    async def _prepare_cb(
        cb: CallbackQuery,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
        http: Any,
    ) -> None:
        await outreach_handlers.on_prepare_callback(cb, conn, clock, cfg, client, http)

    @dp.message(Command(re.compile(r"prepare_([0-9a-f]{16})")))
    async def _prepare_link(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
        http: Any,
    ) -> None:
        match = command.regexp_match
        await outreach_handlers.cmd_prepare(
            m, conn, clock, cfg, client, match.group(1) if match else None, http
        )

    @dp.message(Command("prepare"))
    async def _prepare(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
        http: Any,
    ) -> None:
        await outreach_handlers.cmd_prepare(m, conn, clock, cfg, client, command.args, http)

    @dp.callback_query(F.data.startswith("dr:"))
    async def _draft_cb(
        cb: CallbackQuery, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
    ) -> None:
        await outreach_handlers.on_draft_callback(cb, conn, clock, cfg, client)

    @dp.message(Command(re.compile(r"draft_(\d{1,15})")))
    async def _draft_link(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
    ) -> None:
        match = command.regexp_match
        await outreach_handlers.cmd_draft(
            m, conn, clock, cfg, client, match.group(1) if match else None
        )

    @dp.message(Command("draft"))
    async def _draft(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
    ) -> None:
        await outreach_handlers.cmd_draft(m, conn, clock, cfg, client, command.args)

    @dp.message(Command("start"))
    async def _start(m: Message) -> None:
        await handlers.cmd_start(m)

    @dp.message(Command("top"))
    async def _top(m: Message, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig) -> None:
        await handlers.cmd_top(m, conn, clock, cfg)

    @dp.message(Command("saved"))
    async def _saved(m: Message, conn: sqlite3.Connection) -> None:
        await handlers.cmd_saved(m, conn)

    @dp.message(Command("applied"))
    async def _applied(m: Message, conn: sqlite3.Connection) -> None:
        await handlers.cmd_applied(m, conn)

    @dp.message(Command("sources"))
    async def _sources(
        m: Message, conn: sqlite3.Connection, settings: Settings, cfg: AppConfig
    ) -> None:
        await handlers.cmd_sources(m, conn, settings, cfg)

    @dp.message(Command("stats"))
    async def _stats(
        m: Message, conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, clock: Clock
    ) -> None:
        await handlers.cmd_stats(m, conn, settings, cfg, clock)

    @dp.message(Command("boards"))
    async def _boards(m: Message, command: CommandObject, cfg: AppConfig) -> None:
        await handlers.cmd_boards(m, cfg, command.args)

    @dp.message(Command("channels"))
    async def _channels(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        settings: Settings,
        cfg: AppConfig,
        clock: Clock,
    ) -> None:
        await handlers.cmd_channels(m, conn, settings, cfg, clock, command.args)

    @dp.message(Command("pause"))
    async def _pause(m: Message, conn: sqlite3.Connection) -> None:
        await handlers.cmd_pause(m, conn)

    @dp.message(Command("resume"))
    async def _resume(m: Message, conn: sqlite3.Connection) -> None:
        await handlers.cmd_resume(m, conn)

    @dp.message(Command("cv"))
    async def _cv(m: Message, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig) -> None:
        await outreach_handlers.cmd_cv(m, conn, clock, cfg)

    @dp.message(Command("forgetcv"))
    async def _forgetcv(m: Message, conn: sqlite3.Connection, clock: Clock) -> None:
        await outreach_handlers.cmd_forgetcv(m, conn, clock)

    @dp.message(Command("cancel"))
    async def _cancel(m: Message, conn: sqlite3.Connection) -> None:
        await outreach_handlers.cmd_cancel(m, conn)

    async def _pending_cv(message: Message, conn: sqlite3.Connection, clock: Clock) -> bool:
        return outreach_handlers.has_pending_cv(conn, clock)

    async def _pending_contact(message: Message, conn: sqlite3.Connection, clock: Clock) -> bool:
        return outreach_handlers.has_pending_contact(conn, clock)

    @dp.callback_query(F.data.startswith("ac:"))
    async def _addcontact_cb(
        cb: CallbackQuery, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig
    ) -> None:
        await outreach_handlers.on_add_contact_callback(cb, conn, clock, cfg)

    @dp.message(Command(re.compile(r"contacts_([0-9a-f]{16})")))
    async def _contacts_link(
        m: Message, command: CommandObject, conn: sqlite3.Connection, clock: Clock
    ) -> None:
        match = command.regexp_match
        await outreach_handlers.cmd_contacts(m, conn, clock, match.group(1) if match else None)

    @dp.message(Command("contacts"))
    async def _contacts(
        m: Message, command: CommandObject, conn: sqlite3.Connection, clock: Clock
    ) -> None:
        await outreach_handlers.cmd_contacts(m, conn, clock, command.args)

    @dp.message(Command("addcontact"))
    async def _addcontact(
        m: Message,
        command: CommandObject,
        conn: sqlite3.Connection,
        clock: Clock,
        cfg: AppConfig,
        client: Any,
    ) -> None:
        await outreach_handlers.cmd_addcontact(m, conn, clock, cfg, client, command.args)

    @dp.message(F.text, ~F.text.startswith("/"), _pending_contact)
    async def _contact_text(
        m: Message, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
    ) -> None:
        await outreach_handlers.on_contact_text(m, conn, clock, cfg, client)

    # Non-command text is consumed only while a CV is pending; otherwise it falls through.
    @dp.message(F.text, ~F.text.startswith("/"), _pending_cv)
    async def _text(
        m: Message, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
    ) -> None:
        await outreach_handlers.on_text(m, conn, clock, cfg, client)

    @dp.message(F.document, _pending_cv)
    async def _document(
        m: Message, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any, bot: Bot
    ) -> None:
        await outreach_handlers.on_document(m, conn, clock, cfg, client, bot)

    return dp
