"""Bot handlers: feedback callbacks `fb:<action>:<posting_id>` (PLAN 1.10, D-15)."""

from __future__ import annotations

import html
import logging
import math
import sqlite3
import statistics
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest

import src.sources  # noqa: F401  (registers the sources for /sources)
from src import db
from src.clock import Clock
from src.config import TIERS, AppConfig, ScoringConfig, Settings
from src.delivery.digest import item_from_row, load_weights_checked
from src.delivery.format import feedback_keyboard, render_item
from src.llm.budget import LlmBudget
from src.outreach.render import CHUNK_LIMIT, chunk_lines
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
    "/stats - delivery latency and pipeline counters\n"
    "/channels - Telegram channels (/channels add &lt;@h|t.me/h&gt;, /channels remove &lt;h&gt;)\n"
    "/boards - discovered ATS boards (/boards remove &lt;n|source:token&gt;)\n"
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


def reaction_items(rows: list[sqlite3.Row]) -> list[str]:
    """One string per item (title line plus optional URL line), each under the chunk limit."""
    lines = []
    for i, r in enumerate(rows, 1):
        line = f"{i}. <b>{_esc(_clip(r['title'], 200))}</b> — {_esc(_clip(r['company'], 100))}"
        line += f" /prepare_{r['id']}"
        url = r["url"]
        if url.startswith(("http://", "https://")):
            url_line = f"\n{_esc(url)}"
            if len(line) + len(url_line) <= CHUNK_LIMIT:  # a huge URL is dropped, not split
                line += url_line
        lines.append(line)
    return lines


def render_reaction_list(rows: list[sqlite3.Row]) -> str:
    return "\n".join(reaction_items(rows))


async def _cmd_list(message: Any, conn: sqlite3.Connection, action: str, empty: str) -> None:
    rows = db.list_by_reaction(conn, action, LIST_LIMIT)
    if not rows:
        await _say(message, empty)
        return
    for chunk in chunk_lines(reaction_items(rows), CHUNK_LIMIT):  # D-145: stay under 4096
        await _say(message, chunk)


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

    boards: dict[str, dict[str, int]] = {}
    for r in conn.execute(
        "SELECT source, tier, COUNT(*) AS n FROM target_state WHERE target != ''"
        " AND source IN ('greenhouse','lever','ashby') GROUP BY source, tier"
    ):
        boards.setdefault(r["source"], {})[r["tier"] or "unknown"] = r["n"]

    lines = []
    for name in sorted(REGISTRY):
        src = REGISTRY[name]()
        family = source_family(src)
        tier = cfg.source_tier(name, str(getattr(src, "default_tier", "tier2")))
        counts = boards.get(name)
        extra = (
            " (boards: " + ", ".join(f"{t} {n}" for t, n in sorted(counts.items())) + ")"
            if counts
            else ""
        )
        lines.append(
            f"<b>{_esc(name)}</b> [{_esc(family)}] {tier}{extra} {_esc(status[name])}; "
            f"last run: {_esc(last_run(name))}"
        )
    n = len(report.counted)
    lines.append(f"Credential-free source families enabled: {n} (need {MIN_SOURCE_FAMILIES})")
    return "\n".join(lines)


async def cmd_sources(
    message: Any, conn: sqlite3.Connection, settings: Settings, cfg: AppConfig
) -> None:
    await _say(message, render_sources(conn, settings, cfg))


def _dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _median_p90(values: list[float]) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    p90 = ordered[max(math.ceil(0.9 * len(ordered)) - 1, 0)]  # nearest rank
    return f"median {_dur(statistics.median(ordered))}, p90 {_dur(p90)} (n={len(ordered)})"


STATS_DAYS = 7


def render_stats(conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, clock: Clock) -> str:
    """Counters only, no posting text (D-210)."""
    now = clock.now()
    zone = ZoneInfo(settings.timezone)
    since = (now - timedelta(days=STATS_DAYS)).isoformat()
    lines = [f"<b>Stats</b> (latency over {STATS_DAYS} days)"]
    for label, kinds in (("Alerts", ("alert", "fresh_alert")), ("Digest", ("digest",))):
        marks = ",".join("?" * len(kinds))
        rows = conn.execute(
            "SELECT p.source AS source, d.latency_s AS lat FROM deliveries d"
            " JOIN postings p ON p.id=d.posting_id"
            f" WHERE d.kind IN ({marks}) AND d.sent_at >= ? AND d.latency_s IS NOT NULL"
            " ORDER BY p.source",
            (*kinds, since),
        ).fetchall()
        lines.append(f"{label}: {_median_p90([r['lat'] for r in rows])}")
        per: dict[str, list[float]] = {}
        for r in rows:
            per.setdefault(r["source"], []).append(r["lat"])
        for name in sorted(per):
            lines.append(f"  {_esc(name)}: {_median_p90(per[name])}")

    today = now.astimezone(zone).date()
    fresh_today = sum(
        1
        for r in conn.execute("SELECT sent_at FROM deliveries WHERE kind='fresh_alert'")
        if datetime.fromisoformat(r["sent_at"]).astimezone(zone).date() == today
    )
    lines.append(f"Fresh alerts today: {fresh_today}/{cfg.thresholds.fresh_alert_max_per_day}")
    cap = ScoringConfig.from_cfg(cfg).daily_call_cap
    used = LlmBudget(conn, clock, cap, ("score", "score_baseline")).used()
    lines.append(f"Scoring calls today: {used}/{cap}")

    day_ago = (now - timedelta(hours=24)).isoformat()
    per_tier = dict.fromkeys(TIERS, 0)
    for r in conn.execute(
        "SELECT ts.tier AS tier, COUNT(*) AS n FROM postings p"
        " LEFT JOIN target_state ts ON ts.source=p.source AND ts.target=p.target"
        " WHERE p.first_seen_at >= ? GROUP BY ts.tier",
        (day_ago,),
    ):
        per_tier[r["tier"] or "unknown"] = per_tier.get(r["tier"] or "unknown", 0) + r["n"]
    lines.append("New postings, 24h: " + ", ".join(f"{t} {n}" for t, n in per_tier.items()))

    lags = []
    for r in conn.execute(
        "SELECT first_seen_at, source_published_at FROM postings"
        " WHERE source_published_at IS NOT NULL AND first_seen_at >= ?",
        (since,),
    ):
        lag = (
            datetime.fromisoformat(r["first_seen_at"])
            - datetime.fromisoformat(r["source_published_at"])
        ).total_seconds()
        lags.append(max(lag, 0.0))
    lines.append(
        "Source lag: "
        + (f"median {_dur(statistics.median(lags))} (n={len(lags)})" if lags else "n/a")
    )
    return "\n".join(lines)


async def cmd_stats(
    message: Any, conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, clock: Clock
) -> None:
    await _say(message, render_stats(conn, settings, cfg, clock))


BOARDS_USAGE = "Usage: /boards, or /boards remove &lt;n|source:token&gt; (n is the list number)."
NO_BOARDS = "No discovered boards yet."


def render_boards(boards: list[Any]) -> list[str]:
    """Numbered lines, one per active board (company and token escaped, D-214)."""
    lines = []
    for i, b in enumerate(boards, 1):
        day = b.added_at[:10] if b.added_at else "unknown date"
        company = f" - {_esc(_clip(b.company, 120))}" if b.company else ""
        line = f"{i}. {_esc(_clip(b.source, 40))}:{_esc(_clip(b.token, 120))}{company}"
        lines.append(f"{line} ({_esc(day)}, discovered)")
    return lines


async def cmd_boards(message: Any, cfg: AppConfig, args: str | None = None) -> None:
    """List discovered boards or tombstone one. Owner-only via the whitelist middleware."""
    from src.discovery import store

    path = cfg.discovery.boards_file
    parts = (args or "").split()
    boards = [b for b in store.load(path) if b.status == store.ACTIVE]
    if not parts:
        if not boards:
            await _say(message, NO_BOARDS)
            return
        lines = [f"Discovered boards ({len(boards)}):", *render_boards(boards)]
        for chunk in chunk_lines(lines, CHUNK_LIMIT):
            await _say(message, chunk)
        return
    if len(parts) != 2 or parts[0].lower() != "remove":
        await _say(message, BOARDS_USAGE)
        return
    ref = parts[1]
    target = None
    if ref.isdecimal() and ref.isascii():
        n = int(ref)
        if 1 <= n <= len(boards):
            target = boards[n - 1]
    elif ":" in ref:
        source, _, token = ref.partition(":")
        target = next(
            (
                b
                for b in boards
                if b.source == source.lower() and b.token.casefold() == token.casefold()
            ),
            None,
        )
    if target is None:
        await _say(message, BOARDS_USAGE)
        return
    try:
        store.tombstone(path, target.source, target.token)
    except store.StoreCorrupt:
        await _say(message, "The boards file is unreadable; nothing changed.")
        return
    await _say(message, f"Removed {_esc(target.source)}:{_esc(target.token)}.")


CHANNELS_USAGE = (
    "Usage: /channels, /channels add &lt;@handle|t.me/handle&gt;, "
    "or /channels remove &lt;handle&gt;."
)
BAD_CHANNEL = "That is not a valid public channel handle (5-32 letters, digits or _)."


def render_channels(conn: sqlite3.Connection, settings: Settings, cfg: AppConfig) -> list[str]:
    from src.sources.telegram_channels import TelegramChannelsSource, channels, config_channels

    in_config = set(config_channels(cfg))
    reason = TelegramChannelsSource().disabled_reason(settings, cfg, conn)
    polled = {
        r["target"]: r["baselined_at"]
        for r in conn.execute(
            "SELECT target, baselined_at FROM target_state WHERE source='telegram_channels'"
        )
    }
    lines = []
    for i, c in enumerate(channels(cfg, conn), 1):
        origin = "config" if c in in_config else "added"
        if reason:
            status = f"not polling ({reason})"
        elif polled.get(c):
            status = "polling, baselined"
        else:
            status = "polling, first poll pending"
        lines.append(f"{i}. @{_esc(c)} [{origin}] {_esc(status)}")
    return lines


async def cmd_channels(
    message: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    args: str | None = None,
) -> None:
    """List, add or remove Telegram channels at runtime. Never edits config.toml (D-193)."""
    from src.sources.telegram_channels import MAX_CHANNELS, channels, normalize_channel

    parts = (args or "").split()
    if not parts:
        lines = render_channels(conn, settings, cfg)
        if not lines:
            await _say(message, "No Telegram channels. " + CHANNELS_USAGE)
            return
        for chunk in chunk_lines([f"Telegram channels ({len(lines)}):", *lines], CHUNK_LIMIT):
            await _say(message, chunk)
        return
    if len(parts) != 2 or parts[0].lower() not in ("add", "remove"):
        await _say(message, CHANNELS_USAGE)
        return
    handle = normalize_channel(parts[1])
    if handle is None:
        await _say(message, BAD_CHANNEL)
        return
    current = {c.casefold(): c for c in channels(cfg, conn)}
    if parts[0].lower() == "add":
        if handle.casefold() in current:
            await _say(message, f"@{_esc(handle)} is already listed.")
            return
        if len(current) >= MAX_CHANNELS:
            await _say(message, f"At most {MAX_CHANNELS} channels; remove one first.")
            return
        db.set_tg_channel(conn, handle, "added", clock.now())
        await _say(message, f"Added @{_esc(handle)}. Its first poll is a baseline.")
        return
    existing = current.get(handle.casefold())
    if existing is None:
        await _say(message, f"@{_esc(handle)} is not in the list.")
        return
    db.set_tg_channel(conn, existing, "removed", clock.now())
    await _say(message, f"Removed @{_esc(existing)}.")


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
