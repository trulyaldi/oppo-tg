"""Telegram notice when every item in a scoring run fails (D-83).

Without this a broken API key, wrong model id or SDK mismatch only shows up in the
log: the digest just comes out empty. Sent at most once per `REALERT_AFTER` while
the failure persists, and re-armed as soon as a run scores something.
"""

from __future__ import annotations

import html
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from src import db
from src.clock import Clock
from src.llm.budget import TZ

log = logging.getLogger(__name__)

STATE_KEY = "scoring_failure_alerted_at"
REALERT_AFTER = timedelta(hours=6)


def is_total_failure(stats: dict[str, int]) -> bool:
    """Calls failed and nothing got scored. Skipped and capped items are not failures (D-184)."""
    return stats.get("errors", 0) > 0 and stats.get("scored", 0) == 0


def render_failure(
    stats: dict[str, int], last_error: str | None, model: str, stage: str = "scoring"
) -> str:
    error = html.escape(last_error or "unknown error, see the bot log")
    n = stats.get("errors", 0)
    return (
        f"<b>ERROR: {stage} failed</b>\n"
        f"All {n} {stage} attempts in this run failed, "
        "so no new items will reach your digest.\n"
        f"Model: <code>{html.escape(model)}</code>\n"
        f"Last error: <code>{error}</code>\n"
        "Check ANTHROPIC_API_KEY, SCORING_MODEL and the Anthropic status page, "
        "then look at the bot log. I will remind you again in 6 hours if it keeps failing."
    )


def cap_state_key(day: str, stage: str = "scoring") -> str:
    return f"cap_warned:{'score' if stage == 'scoring' else stage}:{day}"


def render_cap_warning(stats: dict[str, int], cap: int, stage: str = "scoring") -> str:
    if stage == "triage":
        return (
            "<b>Triage cap reached</b>\n"
            f"The daily triage cap of {cap} calls is used up, so {stats.get('capped', 0)} "
            "postings wait until tomorrow (Asia/Almaty).\n"
            "Raise <code>[triage] daily_call_cap</code> in config.toml to triage more per day. "
            "Baseline postings are limited by <code>[triage] baseline_max_share</code>."
        )
    return (
        "<b>Scoring cap reached</b>\n"
        f"The daily scoring cap of {cap} calls is used up, so {stats.get('capped', 0)} "
        "postings wait until tomorrow (Asia/Almaty).\n"
        "Raise <code>[scoring] daily_call_cap</code> in config.toml to score more per day. "
        "Baseline postings are limited by <code>[scoring] baseline_max_share</code>."
    )


async def warn_cap_once(
    bot: Any,
    chat_id: int | str,
    conn: sqlite3.Connection,
    clock: Clock,
    stats: dict[str, int],
    cap: int,
    stage: str = "scoring",
) -> bool:
    """One cap warning per Asia/Almaty day, however many runs hit the cap. Never raises."""
    try:
        if stats.get("capped", 0) <= 0:
            return False
        key = cap_state_key(clock.now().astimezone(TZ).date().isoformat(), stage)
        if db.get_state(conn, key):
            return False
        await bot.send_message(chat_id, render_cap_warning(stats, cap, stage), parse_mode="HTML")
        db.set_state(conn, key, clock.now().isoformat())
        return True
    except Exception:
        log.exception("could not send the scoring cap warning")
        return False


async def report_scoring_outcome(
    bot: Any,
    chat_id: int | str,
    conn: sqlite3.Connection,
    clock: Clock,
    stats: dict[str, int],
    last_error: str | None,
    model: str,
    cap: int | None = None,
    stage: str = "scoring",
) -> bool:
    """Alert on total failure (throttled); re-arm on success. Never raises. True if sent.

    `stage` is "scoring" or "triage"; each has its own throttle and cap-warning keys.
    """
    state_key = STATE_KEY if stage == "scoring" else f"{stage}_failure_alerted_at"
    if cap is not None:
        await warn_cap_once(bot, chat_id, conn, clock, stats, cap, stage)
    try:
        if stats.get("scored", 0) > 0:
            if db.get_state(conn, state_key):
                db.set_state(conn, state_key, "")
            return False
        if not is_total_failure(stats):
            return False
        now = clock.now()
        previous = db.get_state(conn, state_key)
        if previous and now - datetime.fromisoformat(previous) < REALERT_AFTER:
            log.warning("scoring failing again, alert suppressed (sent recently)")
            return False
        await bot.send_message(
            chat_id, render_failure(stats, last_error, model, stage), parse_mode="HTML"
        )
        db.set_state(conn, state_key, now.isoformat())
        return True
    except Exception:
        log.exception("could not send the scoring failure alert")
        return False
