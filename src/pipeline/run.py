"""Pipeline wiring."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

import src.sources  # noqa: F401,I001  (registers the sources)
from src import db
from src.clock import Clock
from src.config import AppConfig, Settings
from src.http import PoliteClient
from src.pipeline.dedupe import fingerprint, mark_duplicates
from src.pipeline.filters import apply_filters
from src.scoring.scorer import Scorer
from src.sources import base
from src.sources.base import SourceContext, SourceResult, run_source

log = logging.getLogger(__name__)


async def collect_all(
    conn: sqlite3.Connection,
    http: PoliteClient,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
) -> dict[str, int]:
    """Run all enabled sources concurrently, store postings, dedupe and filter."""
    ctx = SourceContext(http=http, settings=settings, cfg=cfg, clock=clock)
    sources = [f() for f in base.REGISTRY.values()]
    sources = [s for s in sources if s.enabled(settings, cfg)]
    run_ids = {s.name: db.start_source_run(conn, s.name, clock.now()) for s in sources}
    results: list[SourceResult] = list(await asyncio.gather(*(run_source(s, ctx) for s in sources)))
    stats = {"sources": len(sources), "failed": 0, "items": 0, "new": 0}
    for res in results:
        n_new = 0
        for p in res.items:
            if db.upsert_posting(conn, p, fingerprint(p), clock.now()):
                n_new += 1
        db.finish_source_run(
            conn,
            run_ids[res.name],
            clock.now(),
            ok=res.ok,
            n_items=len(res.items),
            n_new=n_new,
            error=res.error,
        )
        stats["items"] += len(res.items)
        stats["new"] += n_new
        stats["failed"] += 0 if res.ok else 1
    stats["duplicates"] = mark_duplicates(conn)
    stats["filtered"] = sum(apply_filters(conn, cfg, clock).values())
    return stats


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
