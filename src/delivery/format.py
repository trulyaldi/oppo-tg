"""Telegram message rendering: items, digest header, feedback keyboard (PLAN 1.10, D-16, D-18)."""

from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.scoring.reason import clip_reason

MAX_ITEM_CHARS = 3500
MAX_CALLBACK_BYTES = 64
MAX_URL_CHARS = 1000
ACTIONS = (("like", "Like"), ("dislike", "Dislike"), ("save", "Save"), ("applied", "Applied"))
ALERT_PREFIX = "<b>ALERT</b> · "
FRESH_PREFIX = "<b>NEW</b> · "

# Aggregators ask for a credit link back (D-178, D-191); no logos are used.
SOURCE_ATTRIBUTION = {
    "remoteok": "Remote OK",
    "jobicy": "Jobicy",
    "arbeitnow": "Arbeitnow",
}

_MAX_TITLE = 90
_MAX_COMPANY = 50
_MAX_CITY = 30
_MAX_MARKER = 24
MAX_LINE1 = 220


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
    city: str = ""
    opportunity_type: str = ""
    marker: str = ""
    contract: int = 2

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Item:
        """Build from a joined postings+scores row (structured deadline wins, D-14, D-262)."""

        def get(key: str) -> object:
            return row[key] if key in row.keys() else None  # noqa: SIM118 - sqlite3.Row

        deadline = row["deadline"] or row["llm_deadline"] or get("ex_deadline")
        try:
            parsed = date.fromisoformat(str(deadline)[:10]) if deadline else None
        except ValueError:
            parsed = None
        scam = str(get("scam") or "none")
        if get("hard_blocker"):
            marker = "hard blocker"
        elif scam not in ("none", "") and scam != "personal_contact_only":
            marker = "possible scam"
        elif scam == "personal_contact_only":
            marker = "personal contact only"
        elif get("visa_barrier") == 1:
            marker = "visa barrier"
        else:
            marker = ""
        return cls(
            posting_id=row["id"],
            title=row["title"] or str(get("ex_title") or ""),
            company=row["company"] or str(get("ex_company") or ""),
            url=row["url"],
            score=row["adjusted_score"],
            reason=row["reason"],
            deadline=parsed,
            red_flags=tuple(json.loads(row["red_flags"] or "[]")),
            base_score=row["base_score"],
            employer_category=row["employer_category"],
            adjustment_detail=row["adjustment_detail"],
            source=str(get("source") or ""),
            hard_blocker=str(get("hard_blocker") or ""),
            city=str(get("location") or get("ex_location") or ""),
            opportunity_type=str(get("opportunity_type") or ""),
            marker=marker,
            contract=2 if get("contract") is None else int(get("contract") or 1),
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


def _fmt_score(score: float | None) -> str:
    value = 0.0 if score is None else score
    return f"{value:.1f}".removesuffix(".0")


def _marker(item: Item, limit: int) -> str:
    """One short red-flag marker: hard blocker > scam > visa barrier > first flag (D-260)."""
    if item.hard_blocker:
        text = "hard blocker"
    elif item.marker:
        text = item.marker
    else:
        text = next((f for f in item.red_flags if f.strip()), "")
    return _clip(text, limit) if text else ""


def _line1(item: Item, lim: tuple[int, int, int, int], prefix: str) -> str:
    title, company, city, marker = lim
    head = f"{_esc(item.flag)} " if item.flag else ""
    first = f"<b>{_esc(_clip(item.title, title))}</b>"
    parts = []
    if co := _clip(item.company, company):
        first += f" — {_esc(co)}"
    parts.append(first)
    if c := _clip(item.city, city):
        parts.append(_esc(c))
    if item.deadline:
        parts.append(f"until {item.deadline.isoformat()}")
    if m := _marker(item, marker):
        parts.append(f"⚠ {_esc(m)}")
    return prefix + head + " · ".join(parts)


def _build(item: Item, reason: str, alert: bool, fresh: bool = False) -> str:
    prefix = (FRESH_PREFIX if fresh else ALERT_PREFIX) if alert else ""
    # Escaping can inflate line 1; shrink the fields until it fits (D-260, <= 220 characters).
    lim = [_MAX_TITLE, _MAX_COMPANY, _MAX_CITY, _MAX_MARKER]
    line1 = _line1(item, tuple(lim), prefix)  # type: ignore[arg-type]
    while len(line1) > MAX_LINE1 and any(x > 8 for x in lim):
        i = lim.index(max(lim))
        lim[i] = max(8, lim[i] - 6)
        line1 = _line1(item, tuple(lim), prefix)  # type: ignore[arg-type]
    base = item.base_score if item.base_score is not None else item.score
    lines = [line1, f"Relevance {_fmt_score(base)}/10: {_esc(reason)}"]
    link = safe_url(item.url)
    credit = SOURCE_ATTRIBUTION.get(item.source)
    if link or credit:
        text = _esc(link) if link else ""
        if credit:
            text += f"{' · ' if text else ''}via {_esc(credit)}"
        lines.append(text)
    return "\n".join(lines)


def render_item(item: Item, alert: bool = False, fresh: bool = False) -> str:
    """Render one item as Telegram HTML, at most MAX_ITEM_CHARS characters (D-260)."""
    reason = " ".join(item.reason.split())
    if item.contract < 2:
        reason = clip_reason(reason)
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
    """One row: Like, Dislike, Save, Applied, Prepare (always present, D-260)."""
    active = active or {}
    row = []
    for action, label in ACTIONS:
        data = f"fb:{action}:{posting_id}"
        if len(data.encode()) > MAX_CALLBACK_BYTES:
            raise ValueError(f"callback data too long: {data!r}")
        text = f"✓ {label}" if active.get(action) else label
        row.append(InlineKeyboardButton(text=text, callback_data=data))
    data = f"pr:{posting_id}"
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data too long: {data!r}")
    row.append(InlineKeyboardButton(text="Prepare", callback_data=data))
    return InlineKeyboardMarkup(inline_keyboard=[row])
