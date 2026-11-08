"""Daily call cap for Telegram channel discovery (D-254, KF-21).

Reuses `LlmBudget` and `llm_usage` with the purposes below. They are not in
`OUTREACH_PURPOSES` and the scoring and triage budgets sum only their own purposes,
so discovery calls never touch those caps.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from src import db
from src.clock import Clock
from src.config import TelegramDiscoveryConfig
from src.llm.budget import TZ, LlmBudget

log = logging.getLogger(__name__)

TG_PURPOSES = ("tg_search", "tg_sample")


def discovery_budget(
    conn: sqlite3.Connection, clock: Clock, cfg: TelegramDiscoveryConfig
) -> LlmBudget:
    return LlmBudget(conn, clock, cfg.daily_call_cap, TG_PURPOSES)


def warned_key(day: str) -> str:
    return f"cap_warned:tg_discovery:{day}"


def render_warning(cap: int) -> str:
    return (
        "<b>Telegram discovery cap reached</b>\n"
        f"The daily cap of {cap} discovery calls is used up, so channel discovery "
        "continues tomorrow (Asia/Almaty).\n"
        "Raise <code>[telegram_discovery] daily_call_cap</code> in config.toml to allow more."
    )


async def reserve_or_warn(
    bot: Any,
    chat_id: int | str,
    conn: sqlite3.Connection,
    clock: Clock,
    budget: LlmBudget,
    purpose: str,
) -> bool:
    """Reserve one call. On refusal send at most one warning per Almaty day; never raises."""
    if budget.reserve(purpose):
        return True
    try:
        key = warned_key(clock.now().astimezone(TZ).date().isoformat())
        if not db.get_state(conn, key):
            await bot.send_message(chat_id, render_warning(budget.cap), parse_mode="HTML")
            db.set_state(conn, key, clock.now().isoformat())
    except Exception:
        log.exception("could not send the telegram discovery cap warning")
    return False
