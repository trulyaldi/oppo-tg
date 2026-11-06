"""Contact parser for posting text: explicit patterns only (N3, D-103).

The posting text is untrusted data. Nothing here follows instructions in it, and a
published email or Telegram handle is never copied out, only flagged in a note.
"""

from __future__ import annotations

import re

from src.contacts.sanitize import NAME_MAX, NOTE_MAX, ROLE_MAX, clean_field
from src.contacts.store import RawContact
from src.text import html_to_text

ADDRESS_NOTE = "posting lists a direct contact address; see the posting"
MAX_CONTACTS = 10

_W = r"[A-ZА-ЯЁ](?:[a-zа-яё'’]|-(?=[A-ZА-ЯЁ])|(?<=-)[A-ZА-ЯЁ])+"
# 2-4 capitalized words, atomic so a fifth capitalized word rejects the match.
_NAME = rf"(?>{_W}(?:[ \t]{_W}){{1,3}})(?![\w@])(?![ \t][A-ZА-ЯЁ])"
_ROLE_PAREN = r"(?:[ \t]*\(([^()\n]{2,80})\))?"
_ROLE_COMMA = r"[ \t]*,[ \t]*(?:(?i:our|the)[ \t]+)?([^\n.;,()@]{2,60})"

_PATTERNS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(rf"(?i:contact(?:[ \t]+person)?)[ \t]*:[ \t]*({_NAME}){_ROLE_PAREN}"), None),
    (re.compile(rf"(?i:contact)[ \t]+({_NAME}){_ROLE_COMMA}"), None),
    (
        re.compile(rf"(?i:reach[ \t]+out[ \t]+to)[ \t]+({_NAME})(?:{_ROLE_COMMA}|{_ROLE_PAREN})"),
        None,
    ),
    (re.compile(rf"(?i:reach[ \t]+out[ \t]+to)[ \t]+({_NAME})"), None),
    (re.compile(rf"(?i:hiring[ \t]+manager)[ \t]*:[ \t]*({_NAME})"), "Hiring manager"),
    (re.compile(rf"(?i:recruiter)[ \t]*:[ \t]*({_NAME})"), "Recruiter"),
    (re.compile(rf"(?i:questions?)[ \t]*\??[ \t]*,?[ \t]*(?i:ask)[ \t]+({_NAME})"), None),
    (re.compile(rf"(?i:контактное[ \t]+лицо)[ \t]*:[ \t]*({_NAME}){_ROLE_PAREN}"), None),
    (re.compile(rf"(?i:пишите)[ \t]+({_NAME}){_ROLE_PAREN}"), None),
)

# Words that make a "name" a department or a phrase, not a person.
_GENERIC = frozenset(
    {
        "us", "team", "resources", "department", "recruitment", "support", "careers",
        "information", "info", "details", "form", "page", "human", "talent", "hiring",
        "our", "the", "apply", "now", "website", "company", "office", "hr", "email",
        "отдел", "команда", "компания", "кадры", "сайт",
    }
)  # fmt: skip

_ADDRESS = re.compile(
    r"[\w.+\-]+\s?(?:@|\[at\]|\(at\))\s?[\w\-]+(?:\.[\w\-]+)+"
    r"|(?<![\w.@])@[A-Za-z][\w]{4,31}\b|\bt\.me/[A-Za-z]\w{4,31}",
    re.IGNORECASE,
)


def _clean_role(role: str | None) -> str:
    role = " ".join((role or "").split())
    if not role or len(role.split()) > 6 or "@" in role or any(c.isdigit() for c in role):
        return ""
    return clean_field(role, ROLE_MAX)


def _valid_name(name: str) -> bool:
    words = [w.casefold() for w in re.split(r"[ \t]+", name)]
    return 2 <= len(words) <= 4 and not any(w in _GENERIC for w in words)


def parse_posting_contacts(text: str) -> list[RawContact]:
    """Return contacts named by explicit patterns in the posting text (at most 10)."""
    if not text:
        return []
    if "<" in text and ">" in text:
        text = html_to_text(text)
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        for pat, fixed_role in _PATTERNS:
            for m in pat.finditer(line):
                name = m.group(1)
                if not _valid_name(name):
                    continue
                role = fixed_role or _clean_role(next((g for g in m.groups()[1:] if g), ""))
                key = name.casefold()
                if key not in found or (role and not found[key][1]):
                    found[key] = (name, role)
    out: list[RawContact] = []
    note = clean_field(ADDRESS_NOTE, NOTE_MAX) if _ADDRESS.search(text) else ""
    for name, role in list(found.values())[:MAX_CONTACTS]:
        out.append(RawContact(name=clean_field(name, NAME_MAX), role=role, note=note))
        note = ""  # the address hint rides on the first contact only
    return out
