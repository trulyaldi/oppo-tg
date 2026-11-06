"""CV storage: only the current version's row is kept (C3, D-122)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime

from src.cv.extract import cv_sha

# Hooks run inside the same transaction and return the number of rows they deleted.
on_new_cv_version: list[Callable[[sqlite3.Connection, int], int]] = []
on_forget_cv: list[Callable[[sqlite3.Connection], int]] = []


def current_cv(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM cv_versions ORDER BY version DESC LIMIT 1").fetchone()


def save_cv(
    conn: sqlite3.Connection,
    text: str,
    sections_json: str,
    source_kind: str,
    model: str,
    now: datetime,
) -> tuple[int, bool]:
    """Store a CV; returns (version, changed). Identical text keeps the current row."""
    sha = cv_sha(text)
    cur = current_cv(conn)
    if cur is not None and cur["sha"] == sha:
        return cur["version"], False
    with conn:
        c = conn.execute(
            "INSERT INTO cv_versions (sha, text, sections, source_kind, parse_model, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (sha, text, sections_json, source_kind, model, now.isoformat()),
        )
        version = c.lastrowid
        assert version is not None
        conn.execute("DELETE FROM cv_versions WHERE version < ?", (version,))
        for hook in on_new_cv_version:
            hook(conn, version)
    return version, True


def forget_cv(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete the CV and everything derived from it; returns row counts."""
    with conn:
        n = conn.execute("DELETE FROM cv_versions").rowcount
        derived = 0
        for hook in on_forget_cv:
            derived += hook(conn)
    return {"cv_versions": n, "derived": derived}
