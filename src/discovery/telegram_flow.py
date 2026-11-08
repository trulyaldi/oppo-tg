"""Telegram channel discovery for the bot: /findchannels, the weekly job and the buttons (KF-23).

Candidates are shown to the user with Add and Ignore buttons. Add writes the handle to the
`tg_channels` overlay (gitignored data/oppo.db) and never joins anything; nothing is added
without the button press (D-256). Channel titles and sample posts are untrusted: they are
clipped and HTML-escaped here and never logged.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src import db
from src.clock import Clock
from src.config import AppConfig, Settings, TelegramDiscoveryConfig
from src.discovery.telegram import SAMPLE_CLIP, Candidate, discover_channels
from src.discovery.telegram_budget import discovery_budget, reserve_or_warn
from src.sources.telegram_channels import (
    MAX_CHANNELS,
    channels,
    default_client_factory,
    flood_remaining,
    get_listener,
    normalize_channel,
    session_exists,
)
from src.sources.telegram_ro import read_only

log = logging.getLogger(__name__)

MIN_KEYWORD_LEN = 4
MAX_KEYWORDS = 3
TITLE_CLIP = 100
CB_ADD = "tc:add:"
CB_IGN = "tc:ign:"
USAGE = (
    "Usage: /findchannels or /findchannels &lt;keyword&gt; (at most 3 keywords, separated by "
    "commas or spaces, each at least 4 characters)."
)
NO_CREDS_TEXT = (
    "Channel discovery needs Telegram credentials: set TG_API_ID and TG_API_HASH and log in "
    "with python -m src.tools.telethon_login."
)
NO_SESSION_TEXT = (
    "Channel discovery needs a Telethon session: run python -m src.tools.telethon_login."
)
NO_NEW_TEXT = "No new channels found."
UNKNOWN_ACTION = "Unknown action"

_LOCK = asyncio.Lock()


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_keywords(args: str | None) -> list[str] | None | str:
    """None for no keywords, a list for valid ones, or an error text (a str)."""
    raw = (args or "").strip()
    if not raw:
        return None
    parts = raw.split(",") if "," in raw else raw.split()
    words = [p.strip() for p in parts if p.strip()]
    if not words or len(words) > MAX_KEYWORDS or any(len(w) < MIN_KEYWORD_LEN for w in words):
        return USAGE
    return words


def render_candidate(c: Candidate) -> str:
    """`@handle — title (N subscribers, job posts ~35%)` plus the escaped, clipped samples."""
    subs = f"{c.subscribers} subscribers" if c.subscribers is not None else "subscribers unknown"
    title = f" — {esc(_clip(c.title, TITLE_CLIP))}" if c.title else ""
    head = f"@{esc(c.handle)}{title} ({subs}, job posts ~{round(c.job_ratio * 100)}%)"
    return "\n".join([head, *[f"• {esc(_clip(s, SAMPLE_CLIP))}" for s in c.samples]])


def candidate_keyboard(handle: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Add", callback_data=f"{CB_ADD}{handle}"),
                InlineKeyboardButton(text="Ignore", callback_data=f"{CB_IGN}{handle}"),
            ]
        ]
    )


def flood_text(left: int) -> str:
    return f"Telegram flood wait: {left}s left. Discovery resumes after it."


def blocked_reason(settings: Settings, conn: sqlite3.Connection, clock: Clock) -> str | None:
    """An explicit reply text when discovery cannot run, else None."""
    if not settings.tg_api_id or not settings.tg_api_hash:
        return NO_CREDS_TEXT
    left = flood_remaining(conn, clock.now())
    if left:
        return flood_text(left)
    listener_up = bool(getattr(get_listener(), "started", False))
    if not listener_up and not session_exists(settings):
        return NO_SESSION_TEXT
    return None


async def _discover(
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    cues: Any,
    queries: list[str] | None,
    factory: Callable[[Settings], Any] | None,
) -> Any:
    """Run one pass on the listener's client or a short-lived own one."""
    listener = get_listener()
    shared = getattr(listener, "client", None) if getattr(listener, "started", False) else None
    owned = shared is None
    client = shared
    if owned:
        client = read_only((factory or default_client_factory)(settings))
    try:
        if owned:
            await client.connect()
            if not await client.is_user_authorized():
                return None
        return await discover_channels(conn, client, cues, cfg, clock, queries)
    finally:
        if owned:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("discovery client disconnect failed", exc_info=True)


async def run_discovery(
    bot: Any,
    chat_id: int | str,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    cues: Any,
    queries: list[str] | None = None,
    factory: Callable[[Settings], Any] | None = None,
) -> tuple[list[Candidate], str | None]:
    """(new candidates, a note to show or None). Sends the cap warning once per day itself."""
    reason = blocked_reason(settings, conn, clock)
    if reason:
        return [], reason
    async with _LOCK:
        try:
            result = await _discover(conn, settings, cfg, clock, cues, queries, factory)
        except Exception as exc:  # noqa: BLE001 - never leak details or post text
            log.warning("channel discovery failed (%s)", type(exc).__name__)
            return [], "Channel discovery failed; see the log."
    if result is None:
        return [], NO_SESSION_TEXT
    note = None
    if result.stopped == "cap":
        dcfg = TelegramDiscoveryConfig.from_cfg(cfg)
        # The budget is spent, so this reserve fails and sends the once-per-day warning.
        await reserve_or_warn(
            bot, chat_id, conn, clock, discovery_budget(conn, clock, dcfg), "tg_search"
        )
        note = "Discovery stopped: the daily call cap is used up."
    elif result.stopped == "flood":
        note = flood_text(flood_remaining(conn, clock.now()))
    return result.candidates, note


async def send_candidates(bot: Any, chat_id: int | str, candidates: list[Candidate]) -> int:
    for c in candidates:
        await bot.send_message(
            chat_id,
            render_candidate(c),
            parse_mode="HTML",
            reply_markup=candidate_keyboard(c.handle),
            disable_web_page_preview=True,
        )
    return len(candidates)


async def weekly_discovery(
    bot: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    cues: Any,
    factory: Callable[[Settings], Any] | None = None,
) -> int:
    """The weekly job: discover and send candidates to the configured chat only."""
    chat_id = settings.telegram_chat_id
    candidates, note = await run_discovery(
        bot, chat_id, conn, settings, cfg, clock, cues, None, factory
    )
    if candidates:
        await send_candidates(bot, chat_id, candidates)
    log.info("weekly channel discovery: %d candidates (%s)", len(candidates), note or "ok")
    return len(candidates)


def parse_callback(data: str | None) -> tuple[str, str] | None:
    """('add'|'ign', handle) for `tc:<action>:<handle>`, else None."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "tc" or parts[1] not in ("add", "ign"):
        return None
    handle = normalize_channel(parts[2])
    if handle is None or handle != parts[2]:
        return None
    return parts[1], handle


async def _clear_buttons(cb: Any) -> None:
    try:
        if cb.message is not None:
            await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 - cosmetic only
        log.debug("could not clear candidate buttons", exc_info=True)


async def on_candidate_callback(
    cb: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, reload: Any
) -> None:
    parsed = parse_callback(getattr(cb, "data", None))
    row = None
    if parsed is not None:
        row = conn.execute(
            "SELECT handle FROM tg_channel_candidates WHERE lower(handle)=lower(?)", (parsed[1],)
        ).fetchone()
    if parsed is None or row is None:
        await cb.answer(UNKNOWN_ACTION)
        return
    action, handle = parsed[0], row["handle"]
    if action == "ign":
        db.set_tg_candidate_status(conn, handle, "ignored")
        await cb.answer(f"Ignored @{handle}")
        await _clear_buttons(cb)
        return
    current = {c.casefold() for c in channels(cfg, conn)}
    if handle.casefold() not in current:
        if len(current) >= MAX_CHANNELS:
            await cb.answer(f"At most {MAX_CHANNELS} channels; remove one first.", show_alert=True)
            return
        db.set_tg_channel(conn, handle, "added", clock.now())
    db.set_tg_candidate_status(conn, handle, "added")
    if reload is not None:
        await reload()
    await cb.answer(f"Added @{handle}; first poll records a baseline")
    await _clear_buttons(cb)
