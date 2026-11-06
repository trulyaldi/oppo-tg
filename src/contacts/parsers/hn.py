"""Contact parser for HN Who-is-hiring comments (N4).

The comment text is untrusted data: only explicit self-descriptions and the N3 contact
patterns are read, nothing in it is followed. Only the public HN handle is used.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.contacts.parsers.posting_text import parse_posting_contacts
from src.contacts.sanitize import NOTE_MAX, ROLE_MAX, clean_field
from src.contacts.store import RawContact
from src.text import html_to_text

log = logging.getLogger(__name__)

ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"
NOTE = "HN Who is hiring poster"
_HANDLE = re.compile(r"[A-Za-z0-9_\-]{1,40}")
_ID = re.compile(r"\d{1,12}")

# (pattern, role). Ordered: the first match wins.
_SELF: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:i['’]?m|i am)\s+(?:the\s+|a\s+|an\s+)?co-?founder\b", re.I), "co-founder"),
    (re.compile(r"\b(?:i['’]?m|i am)\s+(?:the\s+|a\s+)?founder\b", re.I), "founder"),
    (re.compile(r"\bco-?founder here\b", re.I), "co-founder"),
    (re.compile(r"\bfounder here\b", re.I), "founder"),
    (re.compile(r"\b(?i:i['’]?m|i am)\s+(?:the\s+)?(CTO|CEO|COO)\b"), "\x00"),
    (re.compile(r"\b(?:i['’]?m|i am)\s+(?:the\s+)?head of ([A-Za-z ]{2,30})\b", re.I), "\x01"),
    (re.compile(r"\b(?:i['’]?m|i am)\s+hiring\b", re.I), "hiring manager"),
    (re.compile(r"\bя\s+(?:со)?основател[ья]\b", re.I), "основатель"),
)


def _self_role(text: str) -> str:
    for pat, role in _SELF:
        m = pat.search(text)
        if not m:
            continue
        if role == "\x00":
            return m.group(1)
        if role == "\x01":
            return "Head of " + " ".join(m.group(1).split())
        return role
    return ""


def parse_hn_comment(item_json: Any) -> list[RawContact]:
    """Return the poster and any contacts named in the comment text."""
    if not isinstance(item_json, dict):
        return []
    raw = item_json.get("text")
    if not isinstance(raw, str) or not raw.strip():
        return []  # deleted or empty comment
    text = html_to_text(raw) if "<" in raw else raw
    out: list[RawContact] = []
    author = item_json.get("author")
    if isinstance(author, str) and _HANDLE.fullmatch(author):
        role = clean_field(_self_role(text), ROLE_MAX) or "poster"
        out.append(RawContact(f"HN user {author}", role, clean_field(NOTE, NOTE_MAX)))
    seen = {c.name.casefold() for c in out}
    for c in parse_posting_contacts(text):
        if c.name.casefold() not in seen:
            out.append(c)
            seen.add(c.name.casefold())
    return out


async def fetch_hn_contacts(http: Any, external_id: str) -> list[RawContact]:
    """One cached lookup of the comment; any failure yields []."""
    eid = str(external_id).strip()
    if not _ID.fullmatch(eid):
        return []
    try:
        data = await http.get_json(ITEM_URL.format(id=eid), ttl_s=3600)
    except Exception:  # noqa: BLE001 - never let a lookup break /prepare
        log.warning("hn contact fetch failed")
        return []
    return parse_hn_comment(data)
