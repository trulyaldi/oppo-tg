"""User-added postings: `/add <text>` and forwarded messages (KF-24, D-256, D-262).

The text is untrusted data. Nothing is fetched: the URL is only stored. The posting skips the
keyword pre-filter and goes through triage (extraction), post-triage and scoring on its own.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from src import db
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.sources.telegram_channels import (
    COMPANY_RE,
    CTRL_RE,
    INTERN_RE,
    normalize_channel,
)

MAX_TEXT_CHARS = 8000
MANUAL_SOURCE = "manual"
CHANNEL_SOURCE = "telegram_channels"
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAIL = ".,;:!?)]}»”’"


@dataclass(frozen=True)
class ManualPosting:
    posting: Posting
    target: str


def clean_text(text: str) -> str:
    return CTRL_RE.sub("", text or "").strip()


def first_url(text: str) -> str | None:
    """The first http(s) URL in the text, without trailing punctuation; None if there is none."""
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(_TRAIL)
        try:
            Posting(source="x", external_id="x", title="x", company="", url=url)
        except ValueError:
            continue
        return url
    return None


def text_key(text: str) -> str:
    """sha256 of the whitespace-collapsed, casefolded text, first 16 hex characters."""
    norm = " ".join(text.split()).casefold()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def canonical_handle(username: str, known: Iterable[str]) -> str:
    """The stored spelling of a configured or added channel (case-insensitive), else as given."""
    low = username.casefold()
    for k in known:
        if k.casefold() == low:
            return k
    return username


def _fields(text: str) -> tuple[str, str, Kind]:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    m = COMPANY_RE.search(text)
    kind = Kind.INTERNSHIP if INTERN_RE.search(text[:300]) else Kind.JOB
    return first[:120], (m.group(1)[:120] if m else ""), kind


def build_channel_posting(
    text: str, username: str, message_id: int, known: Iterable[str], when: datetime | None
) -> ManualPosting | None:
    """A forwarded public-channel post: the same id a poll of that message would give."""
    handle = normalize_channel(username)
    text = clean_text(text)
    if handle is None or not text:
        return None
    handle = canonical_handle(handle, known)
    title, company, kind = _fields(text)
    p = Posting(
        source=CHANNEL_SOURCE,
        external_id=f"{handle}:{message_id}",
        title=title,
        company=company,
        url=f"https://t.me/{handle}/{message_id}",
        description=text,
        posted_at=when,
        published_at=when,
        kind=kind,
        raw_tags=(handle,),
    )
    return ManualPosting(p, handle)


def build_manual_posting(text: str) -> ManualPosting | None:
    """`/add` text or a non-channel forward; None when the text has no http(s) link."""
    text = clean_text(text)
    url = first_url(text)
    if not text or url is None:
        return None
    title, company, kind = _fields(text)
    p = Posting(
        source=MANUAL_SOURCE,
        external_id=text_key(text),
        title=title,
        company=company,
        url=url,
        description=text,
        kind=kind,
    )
    return ManualPosting(p, "")


def store_manual(conn: sqlite3.Connection, mp: ManualPosting, now: datetime) -> str:
    """Store with baseline=0 and make an unscored row eligible again; returns the posting id."""
    p = mp.posting
    db.upsert_posting(conn, p, fingerprint(p), now, target=mp.target, baseline=False)
    with conn:
        conn.execute(
            "UPDATE postings SET baseline=0,"
            " status=CASE WHEN status='scored' THEN status ELSE 'new' END,"
            " filter_reason=CASE WHEN status='scored' THEN filter_reason ELSE NULL END,"
            " triage_attempts=CASE WHEN status='scored' THEN triage_attempts ELSE 0 END,"
            " score_attempts=CASE WHEN status='scored' THEN score_attempts ELSE 0 END"
            " WHERE id=?",
            (p.id,),
        )
    return p.id
