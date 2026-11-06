"""Contact parser for team/about/organizer pages (N5, D-133).

The page is untrusted data: only structured person markup and name-plus-role cards are
read. Nothing is followed, no link is opened and email addresses are never copied.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.contacts.classify import classify_role
from src.contacts.parsers.posting_text import _W, _valid_name
from src.contacts.sanitize import NAME_MAX, NOTE_MAX, ROLE_MAX, clean_field
from src.contacts.store import RawContact

MAX_PER_PAGE = 20
ROLE_CARD_MAX = 120
_CARD_NAME = re.compile(rf"{_W}(?:\s+{_W}){{1,3}}")
_ORGANIZER = re.compile(r"organi[sz]ers?", re.IGNORECASE)
_PERSON_TYPE = re.compile(r"schema\.org/Person$", re.IGNORECASE)
_NAV_WORDS = frozenset(
    {"contact", "sales", "about", "careers", "pricing", "login", "blog", "press", "support",
     "privacy", "terms", "partners", "products", "solutions", "events", "news", "our"}
)  # fmt: skip
_ROLE_EXTRA = re.compile(r"organi[sz]er|coordinator|\bchair\b|mentor|\bhost\b", re.IGNORECASE)


def _has_role_keyword(text: str) -> bool:
    return classify_role(text)[0] != "other" or bool(_ROLE_EXTRA.search(text))


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _loose_name(name: str) -> bool:
    words = name.split()
    return 2 <= len(words) <= 5 and not any(c.isdigit() or c == "@" for c in name)


def _is_person_type(value: Any) -> bool:
    values = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and v.rsplit("/", 1)[-1] == "Person" for v in values)


def _walk_jsonld(node: Any, out: list[tuple[str, str, bool]], organizer: bool = False) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out, organizer)
    elif isinstance(node, dict):
        if _is_person_type(node.get("@type")):
            title = node.get("jobTitle")
            title = title if isinstance(title, str) else ""
            name = node.get("name")
            if isinstance(name, str):
                out.append((_norm(name), _norm(title), organizer))
        for key, val in node.items():
            if isinstance(val, (dict, list)):
                _walk_jsonld(val, out, organizer or key.lower() == "organizer")


def _microdata(soup: BeautifulSoup) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for el in soup.find_all(attrs={"itemtype": True}):
        if not isinstance(el, Tag) or not _PERSON_TYPE.search(str(el.get("itemtype"))):
            continue
        name = el.find(attrs={"itemprop": "name"})
        title = el.find(attrs={"itemprop": "jobTitle"})
        if isinstance(name, Tag):
            out.append(
                (_norm(name.get_text(" ")), _norm(title.get_text(" ")) if title else "", False)
            )
    return out


def _jsonld(soup: BeautifulSoup) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        _walk_jsonld(data, out)
    return out


def _sibling_text(tag: Tag) -> str:
    sib = tag.next_sibling
    while sib is not None and not str(sib).strip():
        sib = sib.next_sibling
    if sib is None:
        return ""
    text = sib.get_text(" ") if isinstance(sib, Tag) else str(sib)
    return _norm(text).lstrip("-–—:|,·• ").strip()


def _cards(soup: BeautifulSoup) -> list[tuple[str, str, bool]]:
    page_org = any(
        _ORGANIZER.search(h.get_text(" ")) for h in soup.find_all(["h1", "h2", "h3", "h4", "h5"])
    )
    out: list[tuple[str, str, bool]] = []
    for tag in soup.find_all(["h2", "h3", "h4", "h5", "strong"]):
        name = _norm(tag.get_text(" "))
        if not _CARD_NAME.fullmatch(name) or not _valid_name(name):
            continue
        if _NAV_WORDS & {w.casefold() for w in name.split()} or tag.find_parent("nav"):
            continue
        role = _sibling_text(tag)
        if not role or len(role) > ROLE_CARD_MAX or not _has_role_keyword(role):
            continue
        out.append((name, role, page_org))
    return out


def parse_team_page(html: str, page_url: str) -> list[RawContact]:
    """Return up to 20 people listed on a team or organizer page."""
    if not html or not isinstance(html, str):
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        found = _microdata(soup) + _jsonld(soup)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        found += _cards(soup)
    except Exception:  # garbage markup must never break /prepare
        return []
    host = (urlparse(page_url or "").hostname or "").removeprefix("www.")
    seen: set[str] = set()
    out: list[RawContact] = []
    for name, role, organizer in found:
        key = name.casefold()
        if not _loose_name(name) or key in seen:
            continue
        seen.add(key)
        note = f"listed as organizer on {host}" if organizer else f"listed on {host} team page"
        out.append(
            RawContact(
                name=clean_field(name, NAME_MAX),
                role=clean_field(role, ROLE_MAX),
                note=clean_field(note, NOTE_MAX),
            )
        )
        if len(out) >= MAX_PER_PAGE:
            break
    return out
