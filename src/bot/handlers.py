"""Bot handlers: feedback callbacks `fb:<action>:<posting_id>` (PLAN 1.10, D-15)."""

from __future__ import annotations

import html
import logging
import sqlite3
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramBadRequest

import src.sources  # noqa: F401  (registers the sources for /sources)
from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.delivery.digest import item_from_row, load_weights_checked
from src.delivery.format import feedback_keyboard, render_item
from src.ranking.adjust import recompute_adjusted
from src.sources.base import MIN_SOURCE_FAMILIES, REGISTRY, count_report, source_family

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


# --- commands (PLAN 1.10, T22) --------------------------------------------

TOP_DAYS = 14
TOP_LIMIT = 10
LIST_LIMIT = 20
NOTHING_SCORED = "Nothing scored yet."
NOTHING_SAVED = "Nothing saved yet."
NOTHING_APPLIED = "Nothing marked applied yet."
PAUSED_TEXT = "Paused. /pause again or /resume to resume."
RESUMED_TEXT = "Resumed."
START_TEXT = (
    "oppo-tg is running. Commands:\n"
    "/top - best recent opportunities\n"
    "/saved - saved items\n"
    "/applied - items marked applied\n"
    "/sources - source status\n"
    "/pause - pause the digest\n"
    "/resume - resume the digest\n"
    "/prepare &lt;id&gt; - tailoring advice for a posting\n"
    "/contacts &lt;id&gt; - stored contacts for a posting\n"
    "/addcontact &lt;id&gt; - add a contact from pasted text\n"
    "/draft &lt;contact id&gt; - outreach draft to copy (never sent)\n"
    "/cv - upload or paste your CV\n"
    "/forgetcv - delete the stored CV\n"
    "/cancel - cancel a pending input"
)


def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


async def _say(message: Any, text: str, **kw: Any) -> None:
    await message.answer(text, parse_mode="HTML", **kw)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def cmd_start(message: Any) -> None:
    await _say(message, START_TEXT)


async def cmd_top(
    message: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig | None = None
) -> None:
    weights = load_weights_checked(cfg or AppConfig.from_dict({}))[0]
    recompute_adjusted(conn, weights)
    since = (clock.now() - timedelta(days=TOP_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT p.*, s.base_score AS base_score, s.adjusted_score AS adjusted_score,"
        " s.adjusted_score AS score, s.employer_category AS employer_category,"
        " s.adjustment_detail AS adjustment_detail, s.reason AS reason,"
        " s.deadline AS llm_deadline, s.red_flags AS red_flags"
        " FROM postings p JOIN scores s ON s.posting_id=p.id"
        " WHERE scored_at >= ? ORDER BY s.adjusted_score DESC, p.rowid LIMIT ?",
        (since, TOP_LIMIT),
    ).fetchall()
    if not rows:
        await _say(message, NOTHING_SCORED)
        return
    for row in rows:
        await _say(
            message,
            render_item(item_from_row(row, weights)),
            reply_markup=feedback_keyboard(row["id"], db.active_reactions(conn, row["id"])),
        )


def render_reaction_list(rows: list[sqlite3.Row]) -> str:
    lines = []
    for i, r in enumerate(rows, 1):
        line = f"{i}. <b>{_esc(_clip(r['title'], 200))}</b> — {_esc(_clip(r['company'], 100))}"
        line += f" /prepare_{r['id']}"
        url = r["url"]
        if url.startswith(("http://", "https://")):
            line += f"\n{_esc(url)}"
        lines.append(line)
    return "\n".join(lines)


async def _cmd_list(message: Any, conn: sqlite3.Connection, action: str, empty: str) -> None:
    rows = db.list_by_reaction(conn, action, LIST_LIMIT)
    await _say(message, render_reaction_list(rows) if rows else empty)


async def cmd_saved(message: Any, conn: sqlite3.Connection) -> None:
    await _cmd_list(message, conn, "save", NOTHING_SAVED)


async def cmd_applied(message: Any, conn: sqlite3.Connection) -> None:
    await _cmd_list(message, conn, "applied", NOTHING_APPLIED)


def render_sources(conn: sqlite3.Connection, settings: Settings, cfg: AppConfig) -> str:
    report = count_report(settings, cfg)
    runs = {r["source"]: r for r in db.last_source_runs(conn)}
    status: dict[str, str] = {}
    for names in report.counted.values():
        for n in names:
            status[n] = "counted"
    for n, creds in report.credential_gated.items():
        status[n] = "credentials: " + ", ".join(creds)
    for n, reason in report.disabled.items():
        status[n] = f"disabled: {reason}"

    def last_run(name: str) -> str:
        r = runs.get(name)
        if r is None:
            return "never run"
        if r["ok"]:
            return f"ok, {r['n_items']} items, {r['started_at']}"
        return f"failed ({_clip(r['error'] or 'unknown error', 120)}), {r['started_at']}"

    lines = []
    for name in sorted(REGISTRY):
        family = source_family(REGISTRY[name]())
        lines.append(
            f"<b>{_esc(name)}</b> [{_esc(family)}] {_esc(status[name])}; last run: "
            f"{_esc(last_run(name))}"
        )
    n = len(report.counted)
    lines.append(f"Credential-free source families enabled: {n} (need {MIN_SOURCE_FAMILIES})")
    return "\n".join(lines)


async def cmd_sources(
    message: Any, conn: sqlite3.Connection, settings: Settings, cfg: AppConfig
) -> None:
    await _say(message, render_sources(conn, settings, cfg))


async def cmd_pause(message: Any, conn: sqlite3.Connection) -> None:
    if db.get_state(conn, "paused", "0") == "1":
        db.set_state(conn, "paused", "0")
        await _say(message, RESUMED_TEXT)
    else:
        db.set_state(conn, "paused", "1")
        await _say(message, PAUSED_TEXT)


async def cmd_resume(message: Any, conn: sqlite3.Connection) -> None:
    db.set_state(conn, "paused", "0")
    await _say(message, RESUMED_TEXT)
