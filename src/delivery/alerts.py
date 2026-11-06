"""Instant alerts (D-25): score >= alert_score, or deadline within N days."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.delivery.digest import is_paused, item_from_row, load_weights_checked
from src.delivery.format import Item, feedback_keyboard, render_item
from src.ranking.adjust import recompute_adjusted, region_hints
from src.ranking.weights import Weights

log = logging.getLogger(__name__)

DEFAULT_TZ = "Asia/Almaty"


def _fresh_sent_today(conn: sqlite3.Connection, clock: Clock, tz: str) -> int:
    """`fresh_alert` deliveries already sent on the current local day (D-189)."""
    zone = ZoneInfo(tz)
    today = clock.now().astimezone(zone).date()
    n = 0
    for r in conn.execute("SELECT sent_at FROM deliveries WHERE kind='fresh_alert'"):
        if datetime.fromisoformat(r["sent_at"]).astimezone(zone).date() == today:
            n += 1
    return n


def select_alerts(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    clock: Clock,
    tz: str = DEFAULT_TZ,
    weights: Weights | None = None,
) -> list[tuple[Item, str]]:
    """Undelivered scored items to alert, as (item, kind): base score rules (D-97), then fresh."""
    if weights is None:
        weights = load_weights_checked(cfg)[0]
    recompute_adjusted(conn, weights, region_hints(cfg))
    th = cfg.thresholds
    now = clock.now()
    today = now.astimezone(ZoneInfo(tz)).date()
    last = today + timedelta(days=th.alert_deadline_days)
    cutoff = now - timedelta(hours=th.fresh_window_hours)
    out: list[tuple[Item, str]] = []
    fresh: list[tuple[float, Item]] = []
    for row in db.undelivered_scored(conn):
        if row["baseline"]:  # first-fetch backlog is never alerted (D-188)
            continue
        item = item_from_row(row, weights)
        base = item.base_score if item.base_score is not None else item.score
        by_score = base >= th.alert_score
        by_deadline = (
            item.deadline is not None
            and today <= item.deadline <= last
            and base >= th.alert_deadline_min_score
        )
        if by_score or by_deadline:
            out.append((item, "alert"))  # overlap: existing rule wins, no cap use (D-189)
        elif (
            base >= th.fresh_alert_score and datetime.fromisoformat(row["first_seen_at"]) >= cutoff
        ):
            fresh.append((base, item))
    room = max(th.fresh_alert_max_per_day - _fresh_sent_today(conn, clock, tz), 0)
    fresh.sort(key=lambda t: -t[0])  # stable: ties keep best adjusted order
    out.extend((item, "fresh_alert") for _, item in fresh[:room])
    return out


async def send_alerts(
    bot: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
) -> int:
    """Send one ALERT message per qualifying item; returns the number sent."""
    if is_paused(conn):
        log.info("paused: alerts skipped")
        return 0
    chat_id = settings.telegram_chat_id
    sent = 0
    for item, kind in select_alerts(conn, cfg, clock, settings.timezone):
        try:
            msg = await bot.send_message(
                chat_id,
                render_item(item, alert=True, fresh=kind == "fresh_alert"),
                parse_mode="HTML",
                reply_markup=feedback_keyboard(item.posting_id),
            )
        except Exception:
            log.exception("sending alert %s failed", item.posting_id)
            continue
        db.record_delivery(
            conn, item.posting_id, kind, chat_id, getattr(msg, "message_id", None), clock.now()
        )
        sent += 1
    return sent
