"""Digest selection and sending (D-16, D-17)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.delivery.format import Item, feedback_keyboard, render_digest_header, render_item

log = logging.getLogger(__name__)

EMPTY_TEXT = "No new opportunities today."


def is_paused(conn: sqlite3.Connection) -> bool:
    return db.get_state(conn, "paused", "0") == "1"


def select_digest(conn: sqlite3.Connection, cfg: AppConfig) -> list[Item]:
    """Up to digest_max_items undelivered items scoring >= digest_min_score (D-17)."""
    th = cfg.thresholds
    items = [Item.from_row(r) for r in db.undelivered_scored(conn)]
    items = [i for i in items if i.score >= th.digest_min_score]
    items.sort(key=lambda i: (-i.score, i.deadline or date.max))
    return items[: th.digest_max_items]


async def send_digest(
    bot: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
) -> int:
    """Send the digest; returns the number of item messages sent (0 when paused or empty)."""
    if is_paused(conn):
        log.info("paused: digest skipped")
        return 0
    chat_id = settings.telegram_chat_id
    items = select_digest(conn, cfg)
    if not items:
        await bot.send_message(chat_id, EMPTY_TEXT)
        return 0
    day = clock.now().astimezone(ZoneInfo(settings.timezone)).date()
    await bot.send_message(chat_id, render_digest_header(day, len(items)), parse_mode="HTML")
    sent = 0
    for item in items:
        try:
            msg = await bot.send_message(
                chat_id,
                render_item(item),
                parse_mode="HTML",
                reply_markup=feedback_keyboard(item.posting_id),
            )
        except Exception:
            log.exception("sending item %s failed", item.posting_id)
            continue
        db.record_delivery(
            conn, item.posting_id, "digest", chat_id, getattr(msg, "message_id", None), clock.now()
        )
        sent += 1
    return sent
