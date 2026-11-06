"""Digest selection and sending (D-16, D-17)."""

from __future__ import annotations

import html
import logging
import sqlite3
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.delivery.format import Item, feedback_keyboard, render_digest_header, render_item
from src.ranking.adjust import recompute_adjusted
from src.ranking.quotas import pick_with_quotas
from src.ranking.weights import BUILTIN_WEIGHTS, Weights, load_weights

log = logging.getLogger(__name__)

EMPTY_TEXT = "No new opportunities today."


def is_paused(conn: sqlite3.Connection) -> bool:
    return db.get_state(conn, "paused", "0") == "1"


def load_weights_checked(cfg: AppConfig) -> tuple[Weights, str | None]:
    """Weights from cfg.ranking.weights_file, or (BUILTIN_WEIGHTS, error) if unusable (D-91)."""
    path = cfg.ranking.get("weights_file")
    if not path:
        return BUILTIN_WEIGHTS, None
    try:
        return load_weights(path), None
    except Exception as exc:  # noqa: BLE001 - a YAML typo must not stop the digest
        log.warning("weights unusable, using defaults: %s", exc)
        return BUILTIN_WEIGHTS, str(exc)


def select_digest(
    conn: sqlite3.Connection, cfg: AppConfig, weights: Weights | None = None
) -> list[Item]:
    """Quota-picked undelivered items, base score >= digest_min_score, adjusted order (D-95)."""
    if weights is None:
        weights = load_weights_checked(cfg)[0]
    recompute_adjusted(conn, weights)
    floor = cfg.thresholds.digest_min_score
    items = [Item.from_row(r) for r in db.undelivered_scored(conn)]
    items = [i for i in items if (i.base_score if i.base_score is not None else i.score) >= floor]
    items.sort(key=lambda i: (-i.score, i.deadline or date.max, -(i.base_score or 0.0)))
    return pick_with_quotas(items, weights.digest_quotas, lambda i: i.employer_category)


async def _notify_invalid_weights(
    bot: Any, conn: sqlite3.Connection, settings: Settings, clock: Clock, error: str
) -> None:
    day = clock.now().astimezone(ZoneInfo(settings.timezone)).date().isoformat()
    key = f"weights_invalid_notified:{day}"
    if db.get_state(conn, key):
        return
    text = f"weights.yaml invalid, using defaults: {html.escape(error[:200])}"
    try:
        await bot.send_message(settings.telegram_chat_id, text, parse_mode="HTML")
    except Exception:
        log.exception("weights notice failed")
        return
    db.set_state(conn, key, "1")


async def send_digest(
    bot: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
) -> int:
    """Send the digest; returns the number of item messages sent (0 when paused or empty)."""
    weights, weights_error = load_weights_checked(cfg)
    if weights_error:
        await _notify_invalid_weights(bot, conn, settings, clock, weights_error)
    if is_paused(conn):
        log.info("paused: digest skipped")
        return 0
    chat_id = settings.telegram_chat_id
    items = select_digest(conn, cfg, weights)
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
