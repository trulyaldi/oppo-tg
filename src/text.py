"""Text helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from bs4 import BeautifulSoup

from src.models import Posting

# Whole-word intern match: "Intern", "Internship", "Interns", never "Internal"/"International".
INTERN_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)

_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section")


def html_to_text(html: str) -> str:
    """Convert HTML to plain text: one non-empty line per block/line break."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    lines = (" ".join(line.split()) for line in soup.get_text().splitlines())
    return "\n".join(line for line in lines if line)


def content_hash(p: Posting) -> str:
    """sha256 over NFKC-normalized, whitespace-collapsed title, company, location, description."""
    parts = (
        " ".join(unicodedata.normalize("NFKC", v).split())
        for v in (p.title, p.company, p.location, p.description)
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Return text of at most `limit` characters, ending in `suffix` if cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


_KK_LETTERS = frozenset("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
_KK_MIN_CYRILLIC = 0.30


def detect_language(text: str | None) -> str:
    """Deterministic language guess: "kk", "ru" or "en" (D-249).

    Any Kazakh-specific letter gives kk; at least 30% Cyrillic among the letters gives ru;
    anything else is en."""
    text = text or ""
    if any(ch in _KK_LETTERS for ch in text):
        return "kk"
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return "en"
    cyr = sum(1 for ch in letters if "\u0400" <= ch <= "\u04ff")
    return "ru" if cyr / len(letters) >= _KK_MIN_CYRILLIC else "en"
