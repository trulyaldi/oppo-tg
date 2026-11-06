"""Scoring prompt assembly (D-11, D-12)."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from src.models import Posting

HERE = Path(__file__).parent
MAX_DESC = 4000


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_system_blocks(
    examples: dict[str, list[dict[str, Any]]] | None = None,
    *,
    base_dir: Path | None = None,
) -> list[dict]:
    """Static block (profile + rubric, cached) plus an optional feedback block.

    `examples` maps "like"/"dislike" to dicts with title, company, tags.
    """
    d = base_dir or HERE
    static = _read(d / "profile.md") + "\n\n" + _read(d / "rubric.md")
    blocks: list[dict] = [{"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}]
    sections = []
    for key, heading in (("like", "Liked by the user"), ("dislike", "Disliked by the user")):
        items = (examples or {}).get(key) or []
        if items:
            lines = [f"## {heading}"]
            for e in items:
                tags = ", ".join(e.get("tags") or [])
                lines.append(
                    f"- {e.get('title', '')} ({e.get('company', '')})"
                    + (f": {tags}" if tags else "")
                )
            sections.append("\n".join(lines))
    if sections:
        text = "Recent feedback examples; use them to calibrate taste.\n\n" + "\n\n".join(sections)
        blocks.append({"type": "text", "text": text})
    return blocks


def build_user_message(p: Posting, today: date) -> str:
    desc = p.description[:MAX_DESC]
    deadline = p.deadline.isoformat() if p.deadline else "unknown"
    remote = {True: "yes", False: "no", None: "unknown"}[p.remote]
    body = (
        f"Title: {p.title}\nCompany: {p.company}\nKind: {p.kind.value}\n"
        f"Location: {p.location or 'unknown'}\nRemote: {remote}\n"
        f"Source deadline: {deadline}\nURL: {p.url}\n\n{desc}"
    )
    return f"Today is {today.isoformat()}.\n\n<posting>\n{body}\n</posting>"


def prompt_version(base_dir: Path | None = None) -> str:
    d = base_dir or HERE
    h = hashlib.sha256()
    for name in ("profile.md", "rubric.md"):
        h.update((d / name).read_bytes())
    return h.hexdigest()[:12]
