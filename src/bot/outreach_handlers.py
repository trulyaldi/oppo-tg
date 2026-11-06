"""Outreach handlers: /cv, /forgetcv, /cancel and pasted CV text (C4, 8.1.14)."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.bot.handlers import _say
from src.bot.pending import clear_pending, peek_pending, set_pending, take_pending
from src.clock import Clock
from src.config import AppConfig, OutreachConfig
from src.cv.extract import cv_sha, normalize_cv_text
from src.cv.parse import parse_cv_sections
from src.cv.store import current_cv, forget_cv, save_cv
from src.llm.budget import LlmBudget

MIN_CV_CHARS = 200
TOO_SHORT = "That looks too short for a CV; send the full text or a PDF."
CV_PROMPT = (
    "Send your CV as a PDF (max 5 MB, 20 pages) or paste it as text in your next message. "
    "The CV text will be sent to the Anthropic API (Claude) to parse it, and again whenever "
    "you use /prepare or /draft. It is stored only in the local database under data/. "
    "/cancel to stop."
)
NO_SECTIONS = "Could not find CV sections; nothing saved."
CANCELLED = "Cancelled."
NOTHING_PENDING = "Nothing to cancel."
NO_CV = "No CV stored."


def _cv_prompt(version: int | None) -> str:
    if version is None:
        return CV_PROMPT
    return f"{CV_PROMPT}\nCurrent CV: v{version}. Sending a new one replaces it."


async def cmd_cv(message: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig) -> None:
    oc = OutreachConfig.from_cfg(cfg)
    set_pending(conn, "cv", None, clock.now(), oc.pending_input_minutes)
    cur = current_cv(conn)
    await _say(message, _cv_prompt(cur["version"] if cur else None))


async def cmd_cancel(message: Any, conn: sqlite3.Connection) -> None:
    await _say(message, CANCELLED if clear_pending(conn) else NOTHING_PENDING)


async def cmd_forgetcv(message: Any, conn: sqlite3.Connection, clock: Clock) -> None:
    clear_pending(conn)
    counts = forget_cv(conn)
    if not counts["cv_versions"]:
        await _say(message, NO_CV)
        return
    await _say(
        message,
        f"CV deleted: {counts['cv_versions']} stored CV, "
        f"{counts['derived']} cached result(s) cleared.",
    )


def has_pending_cv(conn: sqlite3.Connection, clock: Clock) -> bool:
    p = peek_pending(conn, clock.now())
    return p is not None and p.kind == "cv"


async def ingest_cv_text(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    raw_text: str,
    source_kind: str = "text",
) -> None:
    """Shared by pasted text (C4) and PDF (C5). Never logs CV content."""
    oc = OutreachConfig.from_cfg(cfg)
    text = normalize_cv_text(raw_text, oc.cv_max_chars)
    if len(text) < MIN_CV_CHARS:
        await _say(message, TOO_SHORT)  # pending stays so the user can retry
        return
    take_pending(conn, clock.now())
    cur = current_cv(conn)
    if cur is not None and cur["sha"] == cv_sha(text):
        await _say(message, f"This CV is unchanged (v{cur['version']}).")
        return
    if client is None:
        await _say(message, "Could not parse the CV right now (no LLM client); try again later.")
        return
    budget = LlmBudget(conn, clock, oc.daily_llm_call_cap)
    res = await parse_cv_sections(client, text, oc, budget)
    if res.error == "budget":
        await _say(
            message,
            f"Daily outreach LLM limit reached ({oc.daily_llm_call_cap} calls). "
            "Try tomorrow or raise [outreach].daily_llm_call_cap.",
        )
        return
    if res.error == "no sections":
        await _say(message, NO_SECTIONS)
        return
    if res.error or res.sections is None:
        await _say(message, f"Could not parse the CV right now ({res.error}); try again later.")
        return
    sec = res.sections
    version, changed = save_cv(
        conn, text, sec.model_dump_json(), source_kind, oc.cheap_model, clock.now()
    )
    await _say(
        message,
        f"CV v{version} saved ({len(text)} chars, sha {cv_sha(text)[:8]}): "
        f"education {len(sec.education)}, experience {len(sec.experience)}, "
        f"projects {len(sec.projects)}, skills {len(sec.skills)}. "
        "The CV text was sent to the Anthropic API for parsing.",
    )


async def on_text(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
) -> None:
    await ingest_cv_text(message, conn, clock, cfg, client, message.text or "")
