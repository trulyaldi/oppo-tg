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
from src.contacts.discover import discover_contacts
from src.contacts.links import linkedin_search_urls, yc_company_url
from src.contacts.pasted import extract_with_llm, parse_pasted
from src.contacts.store import RawContact, add_contact, contacts_for
from src.cv.extract import CvInputError, cv_sha, normalize_cv_text, pdf_to_text
from src.cv.parse import parse_cv_sections
from src.cv.store import current_cv, forget_cv, save_cv
from src.delivery.digest import load_weights_checked
from src.llm.budget import LlmBudget
from src.outreach import draft as _draft  # noqa: F401  (registers cache hooks)
from src.outreach import prepare as _prepare  # noqa: F401  (registers cache hooks)
from src.outreach.draft import FALLBACK_REPLY, DraftError, DraftResult
from src.outreach.draft import draft as make_draft
from src.outreach.prepare import PrepareError, prepare
from src.outreach.render import prepare_keyboard, render_contacts, render_draft, render_prepare

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
    _prepare.register_hooks()
    _draft.register_hooks()
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
    _draft.register_hooks()
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


def _is_yc_source(posting: Any) -> bool:
    return posting["source"] == "hn" or (
        posting["source"] == "rss" and str(posting["external_id"]).startswith("HN Jobs:")
    )


async def run_prepare(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    posting_id: str,
    http: Any = None,
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
    weights = load_weights_checked(cfg)[0]
    report = await discover_contacts(conn, http, posting, cfg, clock, weights)
    budget = LlmBudget(conn, clock, oc.daily_llm_call_cap)
    res = await prepare(conn, client, posting_id, cfg, clock, budget)
    if isinstance(res, PrepareError):
        await _say(message, prepare_error_text(res.kind, oc.daily_llm_call_cap))
        return
    contacts = contacts_for(conn, posting_id, clock.now())
    links = linkedin_search_urls(posting["company"], posting["title"])
    if _is_yc_source(posting):
        yc = yc_company_url(posting["company"])
        if yc:
            links.append(("YC companies", yc))
    chunks = render_prepare(
        posting, res.result, contacts, links, res.removed, unreachable=report.unreachable > 0
    )
    kb = prepare_keyboard(posting_id, contacts, links, add_contact=True)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        if last and kb.inline_keyboard:
            await _say(message, chunk, reply_markup=kb)
        else:
            await _say(message, chunk)


async def cmd_prepare(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    ref_text: str | None,
    http: Any = None,
) -> None:
    ref = resolve_ref(conn, ref_text)
    if ref.posting_id is None:
        await _say(message, _esc(ref.error))
        return
    await run_prepare(message, conn, clock, cfg, client, ref.posting_id, http)


async def on_prepare_callback(
    cb: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    http: Any = None,
) -> None:
    """`pr:<posting_id>`: answer the callback first, then do the LLM work."""
    await cb.answer("Preparing...")
    data = cb.data or ""
    posting_id = data[3:] if data.startswith("pr:") else ""
    message = getattr(cb, "message", None)
    if message is None or not posting_id:
        return
    await run_prepare(message, conn, clock, cfg, client, posting_id, http)


# --- /contacts and /addcontact (N7, D-135) ---------------------------------

URL_ONLY = "Paste the person's name and headline with the URL; I never open LinkedIn links."
CONTACT_PROMPT = (
    "Send the person's name and headline (for example: Alex Example - Senior Recruiter at "
    "Acme), or paste their profile text. You may include their LinkedIn URL; I store it but "
    "never open it. Long pasted text is sent to the Anthropic API (Claude) to extract the "
    "name and role. Nothing is ever sent to the person. /cancel to stop."
)
NOT_UNDERSTOOD = (
    "I could not find a name and role. Send it as: Name - Role at Company "
    "(or paste more of their profile text). /cancel to stop."
)


def has_pending_contact(conn: sqlite3.Connection, clock: Clock) -> bool:
    p = peek_pending(conn, clock.now())
    return p is not None and p.kind == "contact" and bool(p.posting_id)


def _link_set(posting: Any) -> list[tuple[str, str]]:
    links = linkedin_search_urls(posting["company"], posting["title"])
    if _is_yc_source(posting):
        yc = yc_company_url(posting["company"])
        if yc:
            links.append(("YC companies", yc))
    return links


async def cmd_contacts(
    message: Any, conn: sqlite3.Connection, clock: Clock, ref_text: str | None
) -> None:
    if not (ref_text or "").strip():
        await _say(
            message, "Usage: /contacts &lt;id&gt; (a posting id or a prefix of 6+ characters)."
        )
        return
    ref = resolve_ref(conn, ref_text.split()[0])
    if ref.posting_id is None:
        await _say(message, _esc(ref.error))
        return
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (ref.posting_id,)).fetchone()
    contacts = contacts_for(conn, ref.posting_id, clock.now())
    kb = prepare_keyboard(ref.posting_id, contacts, _link_set(posting), add_contact=True)
    chunks = render_contacts(posting, contacts)
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await _say(message, chunk, reply_markup=kb)
        else:
            await _say(message, chunk)


async def start_add_contact(
    message: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, posting_id: str
) -> None:
    oc = OutreachConfig.from_cfg(cfg)
    set_pending(conn, "contact", posting_id, clock.now(), oc.pending_input_minutes)
    await _say(message, _esc(CONTACT_PROMPT))


async def cmd_addcontact(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    args: str | None,
) -> None:
    parts = (args or "").strip().split(None, 1)
    if not parts:
        await _say(message, "Usage: /addcontact &lt;id&gt; [name - role, or pasted profile text]")
        return
    ref = resolve_ref(conn, parts[0])
    if ref.posting_id is None:
        await _say(message, _esc(ref.error))
        return
    if len(parts) == 1:
        await start_add_contact(message, conn, clock, cfg, ref.posting_id)
        return
    await store_pasted_contact(message, conn, clock, cfg, client, ref.posting_id, parts[1])


async def on_add_contact_callback(
    cb: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig
) -> None:
    """`ac:<posting_id>`: answer first, then set the pending input."""
    await cb.answer()
    data = cb.data or ""
    ref = resolve_ref(conn, data[3:] if data.startswith("ac:") else "")
    message = getattr(cb, "message", None)
    if message is None or ref.posting_id is None:
        return
    await start_add_contact(message, conn, clock, cfg, ref.posting_id)


async def on_contact_text(
    message: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
) -> None:
    p = peek_pending(conn, clock.now())
    if p is None or p.kind != "contact" or not p.posting_id:
        return
    await store_pasted_contact(
        message, conn, clock, cfg, client, p.posting_id, message.text or "", pending=True
    )


async def store_pasted_contact(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    posting_id: str,
    text: str,
    pending: bool = False,
) -> None:
    """Parse pasted text as data (never fetched, never logged) and store one contact."""
    oc = OutreachConfig.from_cfg(cfg)
    posting = conn.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if posting is None:
        clear_pending(conn)
        await _say(message, "Posting not found.")
        return
    url, raw, needs_llm = parse_pasted(text)
    company = posting["company"]
    if raw is None and not needs_llm:
        # pending stays so the user can retry
        await _say(message, _esc(URL_ONLY if url else NOT_UNDERSTOOD))
        return
    if needs_llm:
        if client is None:
            await _say(message, "Could not read that right now (no LLM client); try again later.")
            return
        budget = LlmBudget(conn, clock, oc.daily_llm_call_cap)
        raw, found_company, err = await extract_with_llm(client, text, oc, budget)
        if err == "budget":
            await _say(
                message,
                f"Daily outreach LLM limit reached ({oc.daily_llm_call_cap} calls). "
                "Try tomorrow or raise [outreach].daily_llm_call_cap.",
            )
            return
        if err == "rejected":
            if pending:
                clear_pending(conn)
            await _say(message, "I could not find a clear name and role in that text; not saved.")
            return
        if err or raw is None:
            await _say(message, f"Could not read that right now ({err}); try again later.")
            return
        company = found_company or company
    assert isinstance(raw, RawContact)
    cid = add_contact(
        conn,
        posting_id,
        raw,
        source_kind="pasted",
        source_url=url or "",
        company=company,
        now=clock.now(),
        ttl_days=oc.contact_ttl_days,
        max_per_posting=oc.max_contacts_per_posting,
    )
    clear_pending(conn)
    if cid is None:
        await _say(message, "Not saved: that contact already exists or the limit is reached.")
        return
    row = conn.execute("SELECT contact_type FROM contacts WHERE id=?", (cid,)).fetchone()
    await _say(message, f"Saved {_esc(row['contact_type'])} contact #{cid}. /draft_{cid}")


# --- /draft (P6, 8.1.15): shows the draft, never sends it anywhere ----------------

NO_CONTACT = "Contact not found or expired."


def draft_error_text(kind: str, cap: int) -> str:
    if kind == "no_cv":
        return NO_CV_REPLY
    if kind == "budget":
        return (
            f"Daily outreach LLM limit reached ({cap} calls). "
            "Try tomorrow or raise [outreach].daily_llm_call_cap."
        )
    if kind == "no_contact":
        return NO_CONTACT
    if kind == "unsupported":
        return FALLBACK_REPLY
    return f"Could not draft right now ({kind}); try again later."


async def run_draft(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    contact_id: int,
) -> None:
    """Reply into the (already whitelisted) chat of `message`. One message, nothing else sent."""
    oc = OutreachConfig.from_cfg(cfg)
    row = conn.execute(
        "SELECT name, contact_type, expires_at FROM contacts WHERE id=?", (contact_id,)
    ).fetchone()
    if row is None:
        await _say(message, NO_CONTACT)
        return
    if current_cv(conn) is None:
        await _say(message, NO_CV_REPLY)
        return
    if client is None:
        await _say(message, "Could not draft right now (no LLM client); try again later.")
        return
    budget = LlmBudget(conn, clock, oc.daily_llm_call_cap)
    res = await make_draft(conn, client, contact_id, cfg, clock, budget)
    if isinstance(res, DraftError):
        await _say(message, draft_error_text(res.kind, oc.daily_llm_call_cap))
        return
    assert isinstance(res, DraftResult)
    await _say(message, render_draft(row["name"], row["contact_type"], res.body, res.removed))


def parse_contact_id(text: str | None) -> int | None:
    s = (text or "").strip().split(None, 1)[0] if (text or "").strip() else ""
    if not s.isascii() or not s.isdigit() or len(s) > 15:
        return None
    return int(s)


async def cmd_draft(
    message: Any,
    conn: sqlite3.Connection,
    clock: Clock,
    cfg: AppConfig,
    client: Any,
    arg: str | None,
) -> None:
    cid = parse_contact_id(arg)
    if cid is None:
        await _say(message, "Usage: /draft &lt;contact id&gt; (the number shown by /contacts).")
        return
    await run_draft(message, conn, clock, cfg, client, cid)


async def on_draft_callback(
    cb: Any, conn: sqlite3.Connection, clock: Clock, cfg: AppConfig, client: Any
) -> None:
    """`dr:<contact_id>`: answer the callback first, then do the LLM work."""
    await cb.answer("Drafting...")
    data = cb.data or ""
    cid = parse_contact_id(data[3:] if data.startswith("dr:") else "")
    message = getattr(cb, "message", None)
    if message is None or cid is None:
        return
    await run_draft(message, conn, clock, cfg, client, cid)
