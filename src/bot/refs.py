"""Posting reference resolution: 16-hex id or unique prefix of at least 6 chars (P4, D-110)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

MIN_PREFIX = 6
_HEX = re.compile(r"[0-9a-f]+")


@dataclass(frozen=True)
class Ref:
    posting_id: str | None
    error: str | None = None


def resolve_ref(conn: sqlite3.Connection, text: str | None) -> Ref:
    ref = (text or "").strip().lower()
    if not ref:
        return Ref(
            None, "Usage: /prepare <id> (a posting id or a prefix of at least 6 characters)."
        )
    if not _HEX.fullmatch(ref):
        return Ref(None, "Posting not found.")
    if len(ref) < MIN_PREFIX:
        return Ref(None, f"Reference too short: use at least {MIN_PREFIX} characters.")
    rows = conn.execute(
        "SELECT id FROM postings WHERE id >= ? AND id < ? LIMIT 2", (ref, ref + "g")
    ).fetchall()
    if not rows:
        return Ref(None, "Posting not found.")
    exact = [r["id"] for r in rows if r["id"] == ref]
    if exact:
        return Ref(exact[0])
    if len(rows) > 1:
        return Ref(None, "Reference is ambiguous: use more characters.")
    return Ref(rows[0]["id"])
