"""Bot handlers: feedback callbacks `fb:<action>:<posting_id>` (PLAN 1.10, D-15)."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from aiogram.exceptions import TelegramBadRequest

from src import db
from src.clock import Clock
from src.delivery.format import feedback_keyboard

log = logging.getLogger(__name__)

UNKNOWN_ACTION = "Unknown action"
_ANSWERS = {
    "like": "Liked",
    "dislike": "Disliked",
    "save": "Saved",
    "applied": "Marked applied",
}


def parse_feedback(data: str | None) -> tuple[str, str] | None:
    """Return (action, posting_id) for valid `fb:` data, else None."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "fb":
        return None
    _, action, posting_id = parts
    if action not in db.REACTION_ACTIONS or not posting_id:
        return None
    return action, posting_id


async def handle_feedback(cb: Any, conn: sqlite3.Connection, clock: Clock) -> None:
    parsed = parse_feedback(cb.data)
    if parsed is None or db.get_posting(conn, parsed[1]) is None:
        await cb.answer(UNKNOWN_ACTION)
        return
    action, posting_id = parsed
    active = db.set_reaction(conn, posting_id, action, clock.now())
    await cb.answer(_ANSWERS[action] if active[action] else f"{_ANSWERS[action]} removed")
    message = getattr(cb, "message", None)
    if message is None or not hasattr(message, "edit_reply_markup"):
        return
    try:
        await message.edit_reply_markup(reply_markup=feedback_keyboard(posting_id, active))
    except TelegramBadRequest as exc:  # e.g. "message is not modified"
        log.warning("edit_reply_markup failed: %s", exc)
