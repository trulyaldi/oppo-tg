"""Compact Telegram HTML for /prepare output, with chunking (P3, D-127)."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.delivery.format import MAX_CALLBACK_BYTES, safe_url

CHUNK_LIMIT = 4000
MAX_DRAFT_BUTTONS = 5
_CLIP = 200
FOOTER = "Nothing is sent anywhere; drafts are for you to copy."


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def _clip(text: Any, limit: int = _CLIP) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _bullet(text: Any) -> str:
    return "• " + _esc(_clip(text))


def _section(title: str, items: Sequence[str]) -> list[str]:
    return [f"<b>{_esc(title)}</b>", *items] if items else []


def _texts(result: Mapping, key: str) -> list[str]:
    return [_bullet(i.get("text", "")) for i in result.get(key) or [] if isinstance(i, Mapping)]


def _contact_line(c: Mapping) -> str:
    try:
        conf = f"{float(c['type_confidence']):.0%}"
    except (KeyError, TypeError, ValueError):
        conf = "?"
    who = _esc(_clip(c["name"], 100))
    role = _clip(c["role"], 100) if c["role"] else ""
    if role:
        who += f", {_esc(role)}"
    line = f"• {_esc(c['contact_type'])} ({conf}): {who}"
    url = safe_url(c["source_url"] or "")
    if url:
        line += f" {_esc(url)}"
    return f"{line} /draft_{int(c['id'])}"


def render_prepare(
    posting: Mapping,
    result: Mapping,
    contacts: Sequence[Mapping],
    links: Sequence[tuple[str, str]],
    removed: int,
    unreachable: bool = False,
) -> list[str]:
    """Return HTML chunks (each <= CHUNK_LIMIT, tags balanced per line and so per chunk).

    `links` are (label, url) pairs; they appear as keyboard buttons, not in the text.
    """
    lines = [
        f"<b>Prepare: {_esc(_clip(posting['title']))}</b> — {_esc(_clip(posting['company'], 100))}"
    ]
    fit = result.get("fit") if isinstance(result.get("fit"), Mapping) else {}
    level = fit.get("level", "weak")
    lines.append(f"Fit: {_esc(level)}")
    lines += [_bullet(p.get("text", "")) for p in fit.get("points") or [] if isinstance(p, Mapping)]
    lines += _section("Emphasize", _texts(result, "emphasize"))
    lines += _section("Cut", _texts(result, "cut"))
    lines += _section("Reorder", _texts(result, "reorder"))
    kws = [
        _bullet(k.get("keyword", ""))
        for k in result.get("keywords") or []
        if isinstance(k, Mapping)
    ]
    lines += _section("Keywords (in your CV and the posting)", kws)
    gaps = [
        _bullet(f"{g.get('requirement', '')}: {g.get('how_to_address', '')}")
        for g in result.get("gaps") or []
        if isinstance(g, Mapping)
    ]
    lines += _section("Gaps and how to address", gaps)
    lines.append("<b>Contacts</b>")
    if contacts:
        lines += [_contact_line(c) for c in contacts]
    else:
        lines.append("none found yet")
    if unreachable:
        lines.append("Some pages could not be reached.")
    if removed > 0:
        lines.append(f"Removed {int(removed)} unsupported statements.")
    lines.append(FOOTER)
    return chunk_lines(lines, CHUNK_LIMIT)


def chunk_lines(lines: Sequence[str], limit: int = CHUNK_LIMIT) -> list[str]:
    """Join whole lines into chunks of at most `limit` chars; a line is never split."""
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for line in lines:
        if len(line) > limit:
            raise ValueError("single line exceeds chunk limit")
        add = len(line) + (1 if cur else 0)
        if cur and size + add > limit:
            chunks.append("\n".join(cur))
            cur, size, add = [], 0, len(line)
        cur.append(line)
        size += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def prepare_keyboard(
    posting_id: str,
    contacts: Sequence[Mapping],
    links: Sequence[tuple[str, str]],
    add_contact: bool = True,
) -> InlineKeyboardMarkup:
    """URL buttons for links, up to 5 Draft buttons (`dr:<id>`), and Add contact (`ac:<id>`)."""
    rows: list[list[InlineKeyboardButton]] = []
    for label, url in links:
        safe = safe_url(url)
        if safe:
            rows.append([InlineKeyboardButton(text=_clip(label, 60), url=safe)])
    for c in list(contacts)[:MAX_DRAFT_BUTTONS]:
        cid = int(c["id"])
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Draft: {c['contact_type']} #{cid}", callback_data=_cb("dr", cid)
                )
            ]
        )
    if add_contact:
        rows.append([InlineKeyboardButton(text="Add contact", callback_data=_cb("ac", posting_id))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cb(prefix: str, ident: Any) -> str:
    data = f"{prefix}:{ident}"
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data too long: {data!r}")
    return data


def render_contacts(posting: Mapping, contacts: Sequence[Mapping]) -> list[str]:
    """Contacts list for `/contacts`; link buttons come from `prepare_keyboard`."""
    lines = [
        f"<b>Contacts: {_esc(_clip(posting['title']))}</b> — {_esc(_clip(posting['company'], 100))}"
    ]
    if contacts:
        lines += [_contact_line(c) for c in contacts]
    else:
        lines.append("none stored yet. Use the buttons below or /addcontact.")
    lines.append(FOOTER)
    return chunk_lines(lines, CHUNK_LIMIT)


DRAFT_LIMIT = 4000


def render_draft(name: Any, contact_type: Any, body: Any) -> str:
    """Header line plus the body in `<pre>`; the raw body is clipped so the result fits 4000."""
    header = (
        f"Draft for {_esc(_clip(name, 100))} ({_esc(contact_type)}). Not sent: copy it yourself."
    )
    text = str(body or "").strip()
    while True:
        out = f"{header}\n<pre>{_esc(text)}</pre>"
        if len(out) <= DRAFT_LIMIT or not text:
            return out
        text = text[: len(text) - max(1, (len(out) - DRAFT_LIMIT) // 5)].rstrip()
