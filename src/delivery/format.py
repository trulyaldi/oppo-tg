"""Telegram message rendering: items, digest header, feedback keyboard (PLAN 1.10, D-16, D-18)."""

from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MAX_ITEM_CHARS = 3500
MAX_CALLBACK_BYTES = 64
MAX_URL_CHARS = 1000
ACTIONS = (("like", "Like"), ("dislike", "Dislike"), ("save", "Save"), ("applied", "Applied"))
ALERT_PREFIX = "<b>ALERT</b>\n"
FRESH_PREFIX = "<b>NEW</b>\n"

# Aggregators ask for a credit link back (D-178, D-191); no logos are used.
SOURCE_ATTRIBUTION = {"remoteok": "Remote OK", "jobicy": "Jobicy"}

_MAX_TITLE = 200
_MAX_COMPANY = 100
_MAX_FLAG = 150
_MAX_FLAGS = 5


@dataclass(frozen=True)
class Item:
    posting_id: str
    title: str
    company: str
    url: str
    score: float
    reason: str
    deadline: date | None = None
    red_flags: tuple[str, ...] = field(default_factory=tuple)
    base_score: float | None = None
    employer_category: str = "unknown"
    adjustment_detail: str = ""
    flag: str = ""
    source: str = ""
    hard_blocker: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Item:
        """Build from a row of `db.undelivered_scored` (structured deadline wins, D-14)."""
        deadline = row["deadline"] or row["llm_deadline"]
        return cls(
            posting_id=row["id"],
            title=row["title"],
            company=row["company"],
            url=row["url"],
            score=row["adjusted_score"],
            reason=row["reason"],
            deadline=date.fromisoformat(deadline) if deadline else None,
            red_flags=tuple(json.loads(row["red_flags"] or "[]")),
            base_score=row["base_score"],
            employer_category=row["employer_category"],
            adjustment_detail=row["adjustment_detail"],
            source=row["source"] if "source" in row.keys() else "",  # noqa: SIM118 - sqlite3.Row
            hard_blocker=(row["hard_blocker"] if "hard_blocker" in row.keys() else None)  # noqa: SIM118
            or "",
        )


def safe_url(url: str) -> str | None:
    """Return the url if it is http(s) with a host and sane length, else None."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_CHARS or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _build(item: Item, reason: str, alert: bool, fresh: bool = False) -> str:
    prefix = f"{_esc(item.flag)} " if item.flag else ""
    if item.base_score is not None and item.adjustment_detail and item.base_score != item.score:
        score = f"{item.score:.1f}/10 (base {item.base_score:.1f}, {_esc(item.adjustment_detail)})"
    else:
        score = f"{item.score:.1f}/10"
    lines = [
        f"{prefix}<b>{_esc(_clip(item.title, _MAX_TITLE))}</b> — "
        f"{_esc(_clip(item.company, _MAX_COMPANY))}",
        f"{score} · {_esc(reason)}",
        f"Deadline: {item.deadline.isoformat() if item.deadline else 'no deadline'}",
    ]
    flags = [_clip(f, _MAX_FLAG) for f in item.red_flags[:_MAX_FLAGS] if f.strip()]
    if item.hard_blocker:
        flags.insert(0, f"Hard blocker: {_clip(item.hard_blocker, _MAX_FLAG)}")
    if flags:
        lines.append("Red flags: " + _esc("; ".join(flags)))
    credit = SOURCE_ATTRIBUTION.get(item.source)
    if credit:
        lines.append(f"via {_esc(credit)}")
    url = safe_url(item.url)
    if url:
        lines.append(_esc(url))
    head = (FRESH_PREFIX if fresh else ALERT_PREFIX) if alert else ""
    return head + "\n".join(lines)


def render_item(item: Item, alert: bool = False, fresh: bool = False) -> str:
    """Render one item as Telegram HTML, at most MAX_ITEM_CHARS characters."""
    reason = " ".join(item.reason.split())
    text = _build(item, reason, alert, fresh)
    if len(text) <= MAX_ITEM_CHARS:
        return text
    # Truncate the raw reason (never the escaped/tagged text) so no tag or entity is cut.
    overflow = len(text) - MAX_ITEM_CHARS
    limit = max(len(_esc(reason)) - overflow, 0)
    reason = _clip(reason, max(limit, 1))
    while len(text := _build(item, reason, alert, fresh)) > MAX_ITEM_CHARS and len(reason) > 1:
        reason = _clip(reason, len(reason) - 10)
    return text


def render_digest_header(day: date, n: int) -> str:
    return f"<b>Daily digest {day.isoformat()}</b> · {n} items"


def feedback_keyboard(
    posting_id: str, active: dict[str, bool] | None = None
) -> InlineKeyboardMarkup:
    active = active or {}
    row = []
    for action, label in ACTIONS:
        data = f"fb:{action}:{posting_id}"
        if len(data.encode()) > MAX_CALLBACK_BYTES:
            raise ValueError(f"callback data too long: {data!r}")
        text = f"✓ {label}" if active.get(action) else label
        row.append(InlineKeyboardButton(text=text, callback_data=data))
    rows = [row]
    if active.get("like") or active.get("save"):
        data = f"pr:{posting_id}"
        if len(data.encode()) > MAX_CALLBACK_BYTES:
            raise ValueError(f"callback data too long: {data!r}")
        rows.append([InlineKeyboardButton(text="Prepare", callback_data=data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
