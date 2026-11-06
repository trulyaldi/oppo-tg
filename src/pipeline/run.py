"""Pipeline wiring."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

import src.sources  # noqa: F401,I001  (registers the sources)
from src import db
from src.clock import Clock
from src.config import AppConfig, ScoringConfig, Settings
from src.http import PoliteClient
from src.llm.budget import LlmBudget
from src.pipeline.dedupe import fingerprint, mark_duplicates
from src.pipeline.filters import apply_filters
from src.pipeline.scoring_alert import report_scoring_outcome
from src.scoring.scorer import Scorer
from src.sources import base
from src.sources.base import SourceContext, Target, TargetResult, run_target, source_targets

log = logging.getLogger(__name__)


async def _run_source_targets(src: Any, ctx: SourceContext) -> list[TargetResult]:
    """Run a source's targets one after another; never raises."""
    try:
        targets = source_targets(src, ctx)
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        log.exception("source %s targets failed", src.name)
        return [
            TargetResult(src.name, Target(src.name, ""), False, [], f"{type(exc).__name__}: {exc}")
        ]
    return [await run_target(src, ctx, t) for t in targets]


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
    by_source = await asyncio.gather(*(_run_source_targets(s, ctx) for s in sources))
    stats = {"sources": len(sources), "failed": 0, "items": 0, "new": 0}
    for source, results in zip(sources, by_source, strict=True):
        n_new = 0
        n_items = 0
        for tr in results:
            state = db.get_target_state(conn, source.name, tr.target.key)
            is_new = state is None or state["baselined_at"] is None
            if tr.ok:
                n_items += len(tr.items)
                for p in tr.items:
                    outcome = db.upsert_posting(
                        conn,
                        p,
                        fingerprint(p),
                        clock.now(),
                        target=tr.target.key,
                        baseline=is_new,
                    )
                    if outcome is db.Upsert.INSERTED:
                        n_new += 1
            db.record_target_attempt(
                conn,
                source.name,
                tr.target.key,
                clock.now(),
                ok=tr.ok,
                error=tr.error,
                tier=tr.target.tier,
                has_validators=tr.has_validators,
            )
            if tr.ok and is_new:
                db.mark_baselined(conn, source.name, tr.target.key, clock.now())
        failed = [tr for tr in results if not tr.ok]
        ok = not (results and len(failed) == len(results))
        db.finish_source_run(
            conn,
            run_ids[source.name],
            clock.now(),
            ok=ok,
            n_items=n_items,
            n_new=n_new,
            error=None if ok else failed[0].error,
        )
        stats["items"] += n_items
        stats["new"] += n_new
        stats["failed"] += 0 if ok else 1
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
    bot: Any | None = None,
    chat_id: int | str | None = None,
    scoring_cfg: ScoringConfig | None = None,
) -> dict[str, int]:
    """Score pending postings, at most `max_calls` API calls (retries count).

    With `bot` and `chat_id`, a run where every posting failed sends a Telegram
    error notice (D-83) instead of failing silently.
    """
    sc = scoring_cfg or ScoringConfig()
    scorer = Scorer(
        client,
        model,
        conn,
        clock,
        max_calls,
        budget=LlmBudget(conn, clock, sc.daily_call_cap, ("score", "score_baseline")),
        baseline_budget=LlmBudget(conn, clock, sc.baseline_cap, ("score_baseline",)),
    )
    stats = await scorer.score_pending(limit if limit is not None else max_calls)
    if bot is not None and chat_id is not None:
        await report_scoring_outcome(
            bot, chat_id, conn, clock, stats, scorer.last_error, model, sc.daily_call_cap
        )
    return stats
