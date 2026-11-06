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

log = logging.getLogger(__name__)

STATE_KEY = "scoring_failure_alerted_at"
REALERT_AFTER = timedelta(hours=6)


def is_total_failure(stats: dict[str, int]) -> bool:
    """Candidates existed (failed or left unscored) but nothing got scored."""
    candidates = stats.get("errors", 0) + stats.get("skipped", 0)
    return candidates > 0 and stats.get("scored", 0) == 0


def render_failure(stats: dict[str, int], last_error: str | None, model: str) -> str:
    error = html.escape(last_error or "unknown error, see the bot log")
    n = stats.get("errors", 0) + stats.get("skipped", 0)
    return (
        "<b>ERROR: scoring failed</b>\n"
        f"All {n} postings in this run failed to score, "
        "so no new items will reach your digest.\n"
        f"Model: <code>{html.escape(model)}</code>\n"
        f"Last error: <code>{error}</code>\n"
        "Check ANTHROPIC_API_KEY, SCORING_MODEL and the Anthropic status page, "
        "then look at the bot log. I will remind you again in 6 hours if it keeps failing."
    )


async def report_scoring_outcome(
    bot: Any,
    chat_id: int | str,
    conn: sqlite3.Connection,
    clock: Clock,
    stats: dict[str, int],
    last_error: str | None,
    model: str,
) -> bool:
    """Alert on total failure (throttled); re-arm on success. Never raises. True if sent."""
    try:
        if stats.get("scored", 0) > 0:
            if db.get_state(conn, STATE_KEY):
                db.set_state(conn, STATE_KEY, "")
            return False
        if not is_total_failure(stats):
            return False
        now = clock.now()
        previous = db.get_state(conn, STATE_KEY)
        if previous and now - datetime.fromisoformat(previous) < REALERT_AFTER:
            log.warning("scoring failing again, alert suppressed (sent recently)")
            return False
        await bot.send_message(chat_id, render_failure(stats, last_error, model), parse_mode="HTML")
        db.set_state(conn, STATE_KEY, now.isoformat())
        return True
    except Exception:
        log.exception("could not send the scoring failure alert")
        return False
