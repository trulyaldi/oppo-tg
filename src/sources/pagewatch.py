"""Fellowship/research page watcher: emit a posting when a page is new or changed (D-31, D-46)."""

from __future__ import annotations

import hashlib
from typing import Any

from bs4 import BeautifulSoup

from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.sources.base import RequestTally, SourceContext, register
from src.text import INTERN_RE, html_to_text

CACHE_TTL_S = 3600.0
MAX_DESC = 3000


def _kind(value: str, name: str) -> Kind:
    try:
        kind = Kind(value)
    except ValueError:
        kind = Kind.OTHER
    if kind == Kind.JOB and INTERN_RE.search(name):
        return Kind.INTERNSHIP
    return kind


def extract_text(html: str, css_selector: str | None = None) -> str:
    """Main text of a page (selector, else <main>, else <body>); scripts/styles dropped."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        el: Any = None
        if css_selector:
            try:
                el = soup.select_one(css_selector)
            except Exception:  # noqa: BLE001 - bad selector falls back to main/body
                el = None
        el = el or soup.find("main") or soup.body or soup
        return html_to_text(str(el))
    except Exception:  # noqa: BLE001 - malformed HTML must not crash the source
        return ""


def content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _pages(cfg: AppConfig) -> list[dict[str, str]]:
    pages = cfg.source("pagewatch").get("pages") or []
    return [
        {
            "url": str(p["url"]),
            "name": str(p.get("name") or p["url"]),
            "company": str(p.get("company") or p.get("name") or p["url"]),
            "kind": str(p.get("kind", "")),
            "css_selector": str(p.get("css_selector") or ""),
        }
        for p in pages
        if isinstance(p, dict) and p.get("url")
    ]


@register("pagewatch")
class PageWatchSource:
    name = "pagewatch"
    description = "Fellowship/research/early-career pages (posts when a page changes)"

    def enabled(self, settings: Settings, cfg: AppConfig) -> bool:
        return cfg.source_enabled("pagewatch") and bool(_pages(cfg))

    async def fetch(self, ctx: SourceContext) -> list[Posting]:
        tally = RequestTally()
        items: list[Posting] = []
        for page in _pages(ctx.cfg):
            try:
                html = await ctx.http.get_text(page["url"], ttl_s=CACHE_TTL_S)
                if html is None:
                    ctx.log.warning("pagewatch %s: no data", page["name"])
                    tally.failure()
                    continue
                tally.success()
                text = extract_text(html, page["css_selector"] or None)
                if not text.strip():
                    ctx.log.warning("pagewatch %s: no text", page["name"])
                    continue
                digest = content_hash(text)
                conn = ctx.http.db
                row = conn.execute(
                    "SELECT content_hash FROM page_snapshots WHERE url = ?", (page["url"],)
                ).fetchone()
                if row is not None and row[0] == digest:
                    continue
                # D-46: snapshot is saved here, before the posting is persisted.
                conn.execute(
                    "INSERT INTO page_snapshots (url, content_hash, seen_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(url) DO UPDATE SET content_hash = excluded.content_hash, "
                    "seen_at = excluded.seen_at",
                    (page["url"], digest, ctx.clock.now().isoformat()),
                )
                conn.commit()
                title = page["name"] if row is None else f"{page['name']}: page updated"
                items.append(
                    Posting(
                        source="pagewatch",
                        external_id=f"{page['url']}:{digest[:12]}",
                        title=title,
                        company=page["company"],
                        url=page["url"],
                        description=text[:MAX_DESC],
                        posted_at=ctx.clock.now(),
                        kind=_kind(page["kind"], page["name"]),
                    )
                )
            except Exception:  # noqa: BLE001 - one bad page must not stop the others
                ctx.log.exception("pagewatch %s failed", page["name"])
                tally.failure()
        tally.raise_if_all_failed()
        return items
