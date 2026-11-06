"""Outreach handlers: /cv, /forgetcv, /cancel and pasted CV text (C4, 8.1.14)."""

from __future__ import annotations

import io
import sqlite3
from typing import Any

from src.bot.handlers import _esc, _say
from src.bot.pending import clear_pending, peek_pending, set_pending, take_pending
from src.bot.refs import resolve_ref
from src.clock import Clock
from src.config import AppConfig, OutreachConfig
from src.cv.extract import CvInputError, cv_sha, normalize_cv_text, pdf_to_text
from src.cv.parse import parse_cv_sections
from src.cv.store import current_cv, forget_cv, save_cv
from src.llm.budget import LlmBudget
from src.outreach import prepare as _prepare  # noqa: F401  (registers cache hooks)
from src.outreach.prepare import PrepareError, prepare
from src.outreach.render import render_prepare

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
    _prepare.register_hooks()
    before = conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0]
    version, changed = save_cv(
        conn, text, sec.model_dump_json(), source_kind, oc.cheap_model, clock.now()
    )
    after = conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0]
    cleared = (
        f" Cleared {before - after} cached results for older CV versions." if before > after else ""
    )
    await _say(
        message,
        f"CV v{version} saved ({len(text)} chars, sha {cv_sha(text)[:8]}): "
        f"education {len(sec.education)}, experience {len(sec.experience)}, "
        f"projects {len(sec.projects)}, skills {len(sec.skills)}. "
        "The CV text was sent to the Anthropic API for parsing." + cleared,
    )


async def on_text(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
) -> None:
    await ingest_cv_text(message, conn, clock, cfg, client, message.text or "")


UNSUPPORTED = "Only PDF or pasted text is supported."


def _is_pdf(doc: Any) -> bool:
    name = (getattr(doc, "file_name", None) or "").lower()
    return getattr(doc, "mime_type", None) == "application/pdf" or name.endswith(".pdf")


async def on_document(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    bot: Any,
) -> None:
    """PDF CV while a /cv is pending. Downloaded into memory only; never logged."""
    oc = OutreachConfig.from_cfg(cfg)
    doc = message.document
    if not _is_pdf(doc):
        await _say(message, UNSUPPORTED)
        return
    if (doc.file_size or 0) > oc.cv_max_bytes:
        await _say(message, f"That file is too big (max {oc.cv_max_bytes // 1_000_000} MB).")
        return
    buf = io.BytesIO()
    try:
        await bot.download(doc, destination=buf)
        text = pdf_to_text(buf.getvalue(), oc.cv_max_pages)
    except CvInputError as e:
        await _say(message, str(e))
        return
    except Exception:
        await _say(message, "Could not download that file; try again.")
        return
    finally:
        buf.close()
    await ingest_cv_text(message, conn, clock, cfg, client, text, source_kind="pdf")


# --- /prepare (P4, 8.1.15) -------------------------------------------------

NO_CV_REPLY = "Upload your CV first with /cv."


def prepare_error_text(kind: str, cap: int) -> str:
    if kind == "no_cv":
        return NO_CV_REPLY
    if kind == "budget":
        return (
            f"Daily outreach LLM limit reached ({cap} calls). "
            "Try tomorrow or raise [outreach].daily_llm_call_cap."
        )
    if kind == "no_posting":
        return "Posting not found."
    return f"Could not prepare right now ({kind}); try again later."


async def run_prepare(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    posting_id: str,
) -> None:
    """Reply into the chat of `message` (already whitelisted) with tailoring advice."""
    oc = OutreachConfig.from_cfg(cfg)
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        await _say(message, "Posting not found.")
        return
    if current_cv(conn) is None:
        await _say(message, NO_CV_REPLY)
        return
    if client is None:
        await _say(message, "Could not prepare right now (no LLM client); try again later.")
        return
    await _say(message, f"Preparing {_esc(posting['title'][:200])}...")
    budget = LlmBudget(conn, clock, oc.daily_llm_call_cap)
    res = await prepare(conn, client, posting_id, cfg, clock, budget)
    if isinstance(res, PrepareError):
        await _say(message, prepare_error_text(res.kind, oc.daily_llm_call_cap))
        return
    # Contacts and links arrive with N6; the contacts table does not exist yet.
    for chunk in render_prepare(posting, res.result, [], [], res.removed):
        await _say(message, chunk)


async def cmd_prepare(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    ref_text: str | None,
) -> None:
    ref = resolve_ref(conn, ref_text)
    if ref.posting_id is None:
        await _say(message, _esc(ref.error))
        return
    await run_prepare(message, conn, clock, cfg, client, ref.posting_id)


async def on_prepare_callback(
    cb: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
) -> None:
    """`pr:<posting_id>`: answer the callback first, then do the LLM work."""
    await cb.answer("Preparing...")
    data = cb.data or ""
    posting_id = data[3:] if data.startswith("pr:") else ""
    message = getattr(cb, "message", None)
    if message is None or not posting_id:
        return
    await run_prepare(message, conn, clock, cfg, client, posting_id)
