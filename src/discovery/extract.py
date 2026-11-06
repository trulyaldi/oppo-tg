"""Extract ATS board references from untrusted URLs and text (D-190, FC-14).

Pure string handling: nothing here ever fetches a URL. The canonical API URL
is rebuilt later from the validated token only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

MAX_URL_LEN = 2048
MAX_URLS_PER_TEXT = 50

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_ASHBY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

RESERVED_SEGMENTS = frozenset(
    {
        "embed", "api", "j", "v1", "v2", "static", "assets", "favicon.ico",
        "robots.txt", "sitemap.xml", "login", "logout", "signup", "health",
        "jobs", "careers", "about", "privacy", "terms", "search",
    }
)  # fmt: skip

# host -> ATS; hosts match exactly (EU variants are deliberately absent).
_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
}
_LOWERCASE = {"greenhouse", "workable"}  # case-insensitive ATSes

_URL_RE = re.compile(r"https?://[^\s<>\"'`\\^{}|]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>"


@dataclass(frozen=True)
class BoardRef:
    source: str
    token: str


def _clean_token(source: str, raw: str) -> str | None:
    if not raw or "%" in raw or not raw.isascii():
        return None
    token = raw.lower() if source in _LOWERCASE else raw
    regex = _ASHBY_RE if source == "ashby" else _TOKEN_RE
    if not regex.match(token) or token.lower() in RESERVED_SEGMENTS:
        return None
    if ".." in token:
        return None
    return token


def board_ref(url: str) -> BoardRef | None:
    """Return the board a URL points at, or None when it is not a safe match."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LEN:
        return None
    if not url.isascii() or any(ord(c) < 33 for c in url):
        return None
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        netloc = parts.netloc
        # Port access validates the number; ":" in netloc rejects any explicit port.
        _ = parts.port
    except ValueError:
        return None
    if scheme not in ("http", "https") or not netloc:
        return None
    if "@" in netloc or ":" in netloc or "[" in netloc:
        return None
    host = netloc.lower().rstrip(".")
    source = _HOSTS.get(host)
    if source is None:
        return None
    segments = [s for s in parts.path.split("/") if s != ""]
    if not segments:
        return None
    first = segments[0]
    if source == "greenhouse" and first.lower() == "embed":
        if host != "boards.greenhouse.io":
            return None
        values = parse_qs(parts.query).get("for") or []
        token = _clean_token(source, values[0]) if values else None
    else:
        token = _clean_token(source, first)
    return BoardRef(source, token) if token else None


def find_board_refs(text: str) -> list[BoardRef]:
    """Deduplicated board refs found in free text (<=50 URLs scanned)."""
    if not isinstance(text, str) or not text:
        return []
    out: list[BoardRef] = []
    seen: set[BoardRef] = set()
    for count, match in enumerate(_URL_RE.finditer(text)):
        if count >= MAX_URLS_PER_TEXT:
            break
        url = match.group(0).rstrip(_TRAILING)
        ref = board_ref(url)
        if ref is not None and ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out
