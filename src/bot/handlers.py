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
from src.config import TIERS, AppConfig, ScoringConfig, Settings, TriageConfig
from src.cues import fold
from src.delivery.digest import item_from_row, load_weights_checked
from src.delivery.format import SOURCE_ATTRIBUTION, feedback_keyboard, render_item
from src.llm.budget import LlmBudget
from src.outreach.render import CHUNK_LIMIT, chunk_lines
from src.pipeline import manual
from src.pipeline.filters import load_filter_cues
from src.pipeline.run import pipeline_lock, score_pending
from src.ranking.adjust import blocker_penalty, recompute_adjusted, region_hints
from src.ranking.weights import bucket
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
    "/search &lt;words&gt; - search scored postings from the last 30 days\n"
    "/saved - saved items\n"
    "/applied - items marked applied\n"
    "/sources - source status\n"
    "/stats - delivery latency and pipeline counters\n"
    "/channels - Telegram channels (/channels add &lt;@h|t.me/h&gt;, /channels remove &lt;h&gt;)\n"
    "/findchannels [keywords] - find new Telegram channels (Add or Ignore buttons)\n"
    "/add &lt;text with link&gt; - score a posting you paste (or just forward a post)\n"
    "/boards - discovered ATS boards (/boards remove &lt;n|source:token&gt;)\n"
    "/pause - pause the digest\n"
    "/resume - resume the digest\n"
    "/prepare &lt;id&gt; - tailoring advice for a posting\n"
    "/contacts &lt;id&gt; - stored contacts for a posting\n"
    "/addcontact &lt;id&gt; - add a contact from pasted text\n"
    "/draft &lt;contact id&gt; - outreach draft to copy (never sent)\n"
    "/cv - upload or paste your CV\n"
    "/cvreview - review of your stored CV (/cvreview refresh to redo)\n"
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
    c = cfg or AppConfig.from_dict({})
    recompute_adjusted(conn, weights, region_hints(c), blocker_penalty(c))
    since = (clock.now() - timedelta(days=TOP_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT p.*, s.base_score AS base_score, s.adjusted_score AS adjusted_score,"
        " s.adjusted_score AS score, s.employer_category AS employer_category,"
        " s.adjustment_detail AS adjustment_detail, s.reason AS reason,"
        " s.deadline AS llm_deadline, s.red_flags AS red_flags,"
        " s.visa_barrier AS visa_barrier, s.contract AS contract"
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


SEARCH_DAYS = 30
SEARCH_LIMIT = 5
SEARCH_MAX_TERMS = 30
SEARCH_USAGE = "Usage: /search &lt;words&gt;, for example /search internship"
NOTHING_FOUND = "Nothing found."


def search_terms(cues: Any, args: str) -> list[str]:
    """Query words, each expanded to its cue group (D-261), de-duplicated, at most 30 terms."""
    out: list[str] = []
    seen: set[str] = set()
    for word in args.split():
        for term in cues.expand(word):
            key = fold(term)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
    return out[:SEARCH_MAX_TERMS]


def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def cmd_search(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig | None = None,
    args: str | None = None,
) -> None:
    """Local search over scored postings of the last 30 days; no LLM, no network (KF-29)."""
    c = cfg or AppConfig.from_dict({})
    terms = search_terms(load_filter_cues(c), args or "")
    if not terms:
        await _say(message, SEARCH_USAGE)
        return
    weights = load_weights_checked(c)[0]
    recompute_adjusted(conn, weights, region_hints(c), blocker_penalty(c))
    conn.create_function("fold_text", 1, lambda v: fold(v) if v else "", deterministic=True)
    hay = (
        "fold_text(p.title) || char(10) || fold_text(p.company) || char(10) ||"
        " fold_text(p.ex_title) || char(10) || fold_text(p.description)"
    )
    where = " OR ".join(f"{hay} LIKE ? ESCAPE '\\'" for _ in terms)
    since = (clock.now() - timedelta(days=SEARCH_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT p.*, s.base_score AS base_score, s.adjusted_score AS adjusted_score,"
        " s.adjusted_score AS score, s.employer_category AS employer_category,"
        " s.adjustment_detail AS adjustment_detail, s.reason AS reason,"
        " s.deadline AS llm_deadline, s.red_flags AS red_flags,"
        " s.visa_barrier AS visa_barrier, s.contract AS contract"
        " FROM postings p JOIN scores s ON s.posting_id=p.id"
        f" WHERE p.first_seen_at >= ? AND ({where})"
        " ORDER BY s.adjusted_score DESC, p.rowid LIMIT ?",
        (since, *[f"%{_like_escape(t)}%" for t in terms], SEARCH_LIMIT),
    ).fetchall()
    if not rows:
        await _say(message, NOTHING_FOUND)
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
        credit = SOURCE_ATTRIBUTION.get(r["source"])
        if credit:  # aggregator credit, as in the digest (FIX-51, D-284)
            line += f" (via {_esc(credit)})"
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


def render_sources(
    conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, now: Any = None
) -> str:
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
    flood = _flood_note(conn, now)
    if flood:
        lines.append(_esc(flood))
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

    lines.extend(_latency_lines(conn, since))

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
    tcfg = TriageConfig.from_cfg(cfg)
    day = str(today)
    yes = no = 0
    for r in conn.execute("SELECT relevant, created_at FROM triage_results"):
        if datetime.fromisoformat(r["created_at"]).astimezone(zone).date().isoformat() == day:
            yes += bool(r["relevant"])
            no += not r["relevant"]
    t_errors = conn.execute("SELECT COUNT(*) FROM postings WHERE status='triage_error'").fetchone()[
        0
    ]
    t_used = LlmBudget(conn, clock, tcfg.daily_call_cap, ("triage", "triage_baseline")).used()
    lines.append(
        f"Triage today: yes {yes}, no {no}, errors (all time) {t_errors}, "
        f"calls {t_used}/{tcfg.daily_call_cap}" + ("" if tcfg.enabled else " (off)")
    )

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
    generic = conn.execute(
        "SELECT COUNT(*) FROM scores WHERE reason_generic=1 AND scored_at >= ?", (since,)
    ).fetchone()[0]
    lines.append(f"Generic reasons ({STATS_DAYS} days): {generic}")
    from src.sources.telegram_channels import flood_until

    until = flood_until(conn)
    if until is not None and until > now:
        lines.append(f"Telegram flood wait until {until.astimezone(zone):%H:%M}")
    return "\n".join(lines)


def _latency_lines(conn: sqlite3.Connection, since: str) -> list[str]:
    """Alert latency per kind, employer region and tier (KF-30, D-253)."""
    rows = conn.execute(
        "SELECT d.kind AS kind, d.latency_s AS lat, p.source AS source, ts.tier AS tier,"
        " sc.employer_category AS cat FROM deliveries d JOIN postings p ON p.id=d.posting_id"
        " LEFT JOIN target_state ts ON ts.source=p.source AND ts.target=p.target"
        " LEFT JOIN scores sc ON sc.posting_id=p.id"
        " WHERE d.kind IN ('alert','fresh_alert') AND d.sent_at >= ? AND d.latency_s IS NOT NULL",
        (since,),
    ).fetchall()
    out = []
    for kind in ("alert", "fresh_alert"):
        out.append(f"Latency {kind}: {_median_p90([r['lat'] for r in rows if r['kind'] == kind])}")
    for region in ("kz", "intl"):
        vals = [r["lat"] for r in rows if bucket(r["cat"]) == region]
        out.append(f"Latency {region}: {_median_p90(vals)}")
    tiers: dict[str, list[float]] = {t: [] for t in (*TIERS, "manual")}
    for r in rows:
        tier = "manual" if r["source"] == "manual" else (r["tier"] or "unknown")
        tiers.setdefault(tier, []).append(r["lat"])
    for tier, vals in tiers.items():
        if vals or tier in ("tg", "manual"):
            out.append(f"Latency tier {tier}: {_median_p90(vals)}")
    return out


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


def _flood_note(conn: sqlite3.Connection, now: Any = None) -> str:
    """'telegram flood wait: 132s left' while Telethon reads are paused (KF-19), else ''."""
    from src.clock import SystemClock
    from src.sources.telegram_channels import flood_remaining

    left = flood_remaining(conn, now or SystemClock().now())
    return f"telegram flood wait: {left}s left" if left else ""


def render_channels(
    conn: sqlite3.Connection, settings: Settings, cfg: AppConfig, now: Any = None
) -> list[str]:
    from src.sources.telegram_channels import (
        TelegramChannelsSource,
        channels,
        config_channels,
        live_channels,
    )

    live = {c.casefold() for c in live_channels()}
    in_config = set(config_channels(cfg))
    reason = TelegramChannelsSource().disabled_reason(settings, cfg, conn)
    polled = {
        r["target"]: r["baselined_at"]
        for r in conn.execute(
            "SELECT target, baselined_at FROM target_state WHERE source='telegram_channels'"
        )
    }
    ents = db.tg_entities(conn)
    lines = []
    flood = _flood_note(conn, now)
    if flood:
        lines.append(_esc(flood))
    for i, c in enumerate(channels(cfg, conn), 1):
        origin = "config" if c in in_config else "added"
        ent = ents.get(c.casefold())
        if c.casefold() in live:
            status = "live"
        elif ent is not None and ent["status"] == "private":
            status = "private: join it yourself or remove it"
        elif ent is not None and ent["status"] == "invalid":
            status = "invalid"
        elif reason:
            status = f"not polling ({reason})"
        elif polled.get(c):
            status = "polling, baselined"
        else:
            status = "polling, first poll pending"
        lines.append(f"{i}. @{_esc(c)} [{origin}] {_esc(status)}")
    return lines


async def _reload_listener() -> None:
    """Tell the live listener the channel list changed (FC-24); never raises."""
    from src.sources.telegram_channels import get_listener

    listener = get_listener()
    if listener is None:
        return
    try:
        await listener.reload()
    except Exception:  # noqa: BLE001
        log.exception("live listener reload failed")


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
        lines = render_channels(conn, settings, cfg, clock.now())
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
        await _reload_listener()
        await _say(message, f"Added @{_esc(handle)}. Its first poll is a baseline.")
        return
    existing = current.get(handle.casefold())
    if existing is None:
        await _say(message, f"@{_esc(handle)} is not in the list.")
        return
    db.set_tg_channel(conn, existing, "removed", clock.now())
    await _reload_listener()
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


ADD_USAGE = (
    "Usage: /add &lt;posting text, including its link&gt;. You can also forward a post to me. "
    "I never open the link."
)
NO_LINK_TEXT = "Include the posting link (I never open it to read the page)."
TOO_LONG_TEXT = f"That text is too long (at most {manual.MAX_TEXT_CHARS} characters)."
NO_LLM_TEXT = "Could not score this right now (no LLM client); try again later."
FAILED_TEXT = "Could not score this right now; try again later."
TRIAGE_CAP_TEXT = "Not scored: the daily triage cap is reached. Try again tomorrow."
SCORE_CAP_TEXT = "Not scored: the daily scoring cap is reached. Try again tomorrow."


def _scored_item_row(conn: sqlite3.Connection, posting_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.base_score AS base_score, s.adjusted_score AS adjusted_score,"
        " s.adjusted_score AS score, s.employer_category AS employer_category,"
        " s.adjustment_detail AS adjustment_detail, s.reason AS reason,"
        " s.deadline AS llm_deadline, s.red_flags AS red_flags,"
        " s.visa_barrier AS visa_barrier, s.contract AS contract"
        " FROM postings p JOIN scores s ON s.posting_id=p.id WHERE p.id=?",
        (posting_id,),
    ).fetchone()


def _not_scored_text(
    conn: sqlite3.Connection, cfg: AppConfig, clock: Clock, row: sqlite3.Row | None
) -> str:
    if row is not None and row["status"] in ("filtered", "duplicate"):
        return f"Not scored: filtered ({_esc(row['filter_reason'] or row['status'])})."
    tcfg, sc = TriageConfig.from_cfg(cfg), ScoringConfig.from_cfg(cfg)
    if row is not None and row["status"] == "new" and tcfg.enabled:
        used = LlmBudget(conn, clock, tcfg.daily_call_cap, ("triage", "triage_baseline")).used()
        if used >= tcfg.daily_call_cap:
            return TRIAGE_CAP_TEXT
    if LlmBudget(conn, clock, sc.daily_call_cap, ("score", "score_baseline")).used() >= (
        sc.daily_call_cap
    ):
        return SCORE_CAP_TEXT
    return FAILED_TEXT


async def add_user_posting(
    message: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    client: Any,
    mp: manual.ManualPosting,
) -> None:
    """Store, run triage, post-triage and scoring for this posting only, and reply (D-256).

    The delivery is recorded as kind `manual` whatever the outcome, so the item never shows up
    in a later digest or alert.
    """
    pid = manual.store_manual(conn, mp, clock.now())
    chat_id = str(message.chat.id)
    if client is None:
        db.record_delivery(conn, pid, "manual", chat_id, None, clock.now())
        await _say(message, NO_LLM_TEXT)
        return
    async with pipeline_lock():
        try:
            await score_pending(
                conn,
                client,
                settings.scoring_model,
                clock,
                2,
                scoring_cfg=ScoringConfig.from_cfg(cfg),
                triage_cfg=TriageConfig.from_cfg(cfg),
                cues=load_filter_cues(cfg),
                only_id=pid,
            )
        except Exception:
            log.exception("manual posting %s failed", pid)
        weights = load_weights_checked(cfg)[0]
        recompute_adjusted(conn, weights, region_hints(cfg), blocker_penalty(cfg))
        row = _scored_item_row(conn, pid)
        if row is None or row["status"] != "scored":
            db.record_delivery(conn, pid, "manual", chat_id, None, clock.now())
            await _say(message, _not_scored_text(conn, cfg, clock, db.get_posting(conn, pid)))
            return
        sent = await message.answer(
            render_item(item_from_row(row, weights)),
            parse_mode="HTML",
            reply_markup=feedback_keyboard(pid, db.active_reactions(conn, pid)),
        )
        db.record_delivery(
            conn, pid, "manual", chat_id, getattr(sent, "message_id", None), clock.now()
        )


async def cmd_add(
    message: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    client: Any,
    args: str | None,
) -> None:
    text = (args or "").strip()
    if not text:
        await _say(message, ADD_USAGE)
        return
    if len(text) > manual.MAX_TEXT_CHARS:
        await _say(message, TOO_LONG_TEXT)
        return
    mp = manual.build_manual_posting(text)
    if mp is None:
        await _say(message, NO_LINK_TEXT)
        return
    await add_user_posting(message, conn, settings, cfg, clock, client, mp)


def forward_channel_origin(message: Any) -> tuple[str, int] | None:
    """(username, message id) of a forward from a public channel, else None."""
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    username = getattr(chat, "username", None)
    if getattr(origin, "type", None) != "channel" or not username:
        return None
    return str(username), int(origin.message_id)


async def on_forward(
    message: Any,
    conn: sqlite3.Connection,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    client: Any,
) -> None:
    text = message.text or message.caption or ""
    if len(text) > manual.MAX_TEXT_CHARS:
        await _say(message, TOO_LONG_TEXT)
        return
    mp = None
    origin = forward_channel_origin(message)
    if origin is not None:
        from src.sources.telegram_channels import channels

        mp = manual.build_channel_posting(
            text, origin[0], origin[1], channels(cfg, conn), getattr(message, "forward_date", None)
        )
    if mp is None:
        mp = manual.build_manual_posting(text)
    if mp is None:
        await _say(message, NO_LINK_TEXT if text.strip() else "There is no text to add.")
        return
    await add_user_posting(message, conn, settings, cfg, clock, client, mp)
