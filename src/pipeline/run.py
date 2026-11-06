"""Pipeline wiring."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.clock import Clock
from src.scoring.scorer import Scorer


async def score_pending(
    conn: sqlite3.Connection,
    client: Any,
    model: str,
    clock: Clock,
    max_calls: int,
    limit: int | None = None,
) -> dict[str, int]:
    """Score pending postings, at most `max_calls` API calls (retries count)."""
    scorer = Scorer(client, model, conn, clock, max_calls)
    return await scorer.score_pending(limit if limit is not None else max_calls)
