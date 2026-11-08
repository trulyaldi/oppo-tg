"""Pipeline wiring."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import src.sources  # noqa: F401,I001  (registers the sources)
from src import db
from src.clock import Clock
from src.config import TIERS, AppConfig, ScoringConfig, Settings, TriageConfig
from src.cues import Cues, load_cues
from src.http import PoliteClient
from src.llm.budget import LlmBudget
from src.pipeline.dedupe import fingerprint, mark_duplicates
from src.pipeline.filters import apply_filters
from src.pipeline.post_triage import apply_post_triage
from src.pipeline.scoring_alert import report_stage_outcome
from src.scoring.scorer import Scorer
from src.scoring.triage import Triager
from src.sources import base
from src.sources.base import SourceContext, Target, TargetResult, run_target, source_targets

log = logging.getLogger(__name__)


def target_tier(src: Any, cfg: AppConfig, target: Target) -> str:
    """Tier of one target (D-186): `[sources.x] tier` wins, then the target, then the source."""
    default = target.tier or str(getattr(src, "default_tier", "tier2"))
    if default == "tier1" and target.origin != "config":
        default = "tier2"  # only config.toml watchlist boards are tier1
    return cfg.source_tier(src.name, default)


def _source_due(conn: sqlite3.Connection, src: Any, clock: Clock) -> bool:
    """False while the source's `min_poll_minutes` floor has not elapsed (protects ToS)."""
    floor = getattr(src, "min_poll_minutes", None)
    if not floor:
        return True
    row = conn.execute(
        "SELECT MAX(last_attempt_at) AS t FROM target_state WHERE source=?", (src.name,)
    ).fetchone()
    if row is None or row["t"] is None:
        return True
    last = datetime.fromisoformat(row["t"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return clock.now() - last >= timedelta(minutes=floor)


def _validator_wait(
    conn: sqlite3.Connection, src: Any, target: Target, cfg: AppConfig, clock: Clock
) -> bool:
    """True when a tier1 target without validators was fetched ok too recently (D-186)."""
    state = db.get_target_state(conn, src.name, target.key)
    if state is None or state["has_validators"] or state["last_ok_at"] is None:
        return False
    last = datetime.fromisoformat(state["last_ok_at"])
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    wait = timedelta(minutes=cfg.scheduler.no_validator_min_minutes)
    return clock.now() - last < wait


async def _run_source_targets(
    src: Any, ctx: SourceContext, targets: list[Target]
) -> list[TargetResult]:
    """Run a source's targets one after another; never raises.

    A source may own the whole loop via `run_targets` (Telegram: one client, pauses,
    flood-wait stop, FIX-40).
    """
    own = getattr(src, "run_targets", None)
    if callable(own):
        return await own(ctx, targets)
    return [await run_target(src, ctx, t) for t in targets]


def _plan_tier(
    conn: sqlite3.Connection,
    sources: list[Any],
    ctx: SourceContext,
    cfg: AppConfig,
    clock: Clock,
    tier: str,
) -> list[tuple[Any, list[Target]]]:
    """Per source, the due targets of this tier (deduped, floors applied)."""
    plan: list[tuple[Any, list[Target]]] = []
    for source in sources:
        try:
            targets = source_targets(source, ctx)
        except Exception:  # noqa: BLE001 - isolation boundary
            log.exception("source %s targets failed", source.name)
            continue
        seen: set[str] = set()
        mine: list[Target] = []
        for t in targets:
            if t.key in seen:
                continue  # the same token listed twice is fetched once
            seen.add(t.key)
            t = replace(t, tier=target_tier(source, cfg, t))
            if t.tier != tier:
                continue
            if (
                tier == "tier1"
                and not getattr(source, "min_poll_minutes", None)  # own floor (hh, D-252)
                and _validator_wait(conn, source, t, cfg, clock)
            ):
                continue
            mine.append(t)
        if mine and not _source_due(conn, source, clock):
            log.info("source %s skipped: min_poll_minutes floor", source.name)
            continue
        if mine:
            plan.append((source, mine))
    return plan


def process_collected(conn: sqlite3.Connection, cfg: AppConfig, clock: Clock) -> dict[str, int]:
    """Dedupe then filter (run under the pipeline lock when tiers run concurrently)."""
    return {
        "duplicates": mark_duplicates(conn),
        "filtered": sum(apply_filters(conn, cfg, clock).values()),
    }


async def _collect_tier(
    conn: sqlite3.Connection,
    http: PoliteClient,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    tier: str,
) -> dict[str, int]:
    limiter = getattr(http, "limiter", None)
    if limiter is not None:
        limiter.new_run()  # log "host in backoff" once per host per run (FC-07)
    ctx = SourceContext(
        http=http, settings=settings, cfg=cfg, clock=clock, revalidate=(tier == "tier1")
    )
    sources = [f() for f in base.REGISTRY.values()]
    sources = [
        s
        for s in sources
        if (
            s.enabled(settings, cfg, conn)  # runtime channels count (D-193)
            if s.name == "telegram_channels"
            else s.enabled(settings, cfg)
        )
    ]
    plan = _plan_tier(conn, sources, ctx, cfg, clock, tier)
    sources = [s for s, _ in plan]
    run_ids = {s.name: db.start_source_run(conn, s.name, clock.now()) for s in sources}
    by_source = await asyncio.gather(*(_run_source_targets(s, ctx, ts) for s, ts in plan))
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
                if tr.on_stored is not None:
                    try:
                        tr.on_stored()  # post-store hook: cursors advance after the store
                    except Exception:  # noqa: BLE001
                        log.exception("post-store hook failed for %s", tr.target.key)
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
            if tr.ok and is_new and not tr.partial:
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
    return stats


async def collect_tier(
    conn: sqlite3.Connection,
    http: PoliteClient,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
    tier: str,
    *,
    process: bool = True,
) -> dict[str, int]:
    """Collect only the targets of one tier, then dedupe and filter.

    The scheduler passes `process=False` and runs `process_collected` under the
    pipeline lock instead, so collection never waits on the lock (D-207).
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    stats = await _collect_tier(conn, http, settings, cfg, clock, tier)
    if process:
        stats.update(process_collected(conn, cfg, clock))
    return stats


async def collect_all(
    conn: sqlite3.Connection,
    http: PoliteClient,
    settings: Settings,
    cfg: AppConfig,
    clock: Clock,
) -> dict[str, int]:
    """Run every tier once (floors still apply), then dedupe and filter."""
    stats = {"sources": 0, "failed": 0, "items": 0, "new": 0}
    for tier in TIERS:
        for k, v in (await _collect_tier(conn, http, settings, cfg, clock, tier)).items():
            stats[k] += v
    stats.update(process_collected(conn, cfg, clock))
    return stats


_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def pipeline_lock() -> asyncio.Lock:
    """The process-wide PIPELINE_LOCK (one asyncio.Lock per running loop, D-207)."""
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


Step = Callable[[], Awaitable[object]]


async def run_tier_cycle(
    tier: str, *, collect: Step, process: Step, score: Step, alerts: Step
) -> None:
    """One tier job: collect outside the lock; dedupe, filter, score, alerts inside it."""
    try:
        await collect()
    except Exception:
        log.exception("tier %s collect failed", tier)
    async with pipeline_lock():
        for name, step in (("process", process), ("score", score), ("alerts", alerts)):
            try:
                await step()
            except Exception:
                log.exception("tier %s step %s failed", tier, name)


async def triage_pending(
    conn: sqlite3.Connection,
    client: Any,
    clock: Clock,
    triage_cfg: TriageConfig,
    bot: Any | None = None,
    chat_id: int | str | None = None,
) -> dict[str, int]:
    """Triage `new` postings (D-196); warns once per day at the cap, alerts on total failure."""
    triager = Triager(client, conn, clock, triage_cfg)
    stats = await triager.triage_pending()
    if bot is not None and chat_id is not None:
        decided = stats["yes"] + stats["no"] + stats["cached"] + stats["exempt"]
        await report_stage_outcome(
            "triage",
            bot,
            chat_id,
            conn,
            clock,
            {"scored": decided, "errors": stats["errors"], "capped": stats["capped"]},
            triager.last_error,
            triager.model,
            triage_cfg.daily_call_cap,
        )
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
    triage_cfg: TriageConfig | None = None,
    cues: Cues | None = None,
) -> dict[str, int]:
    """Score pending postings, at most `max_calls` API calls (retries count).

    With `bot` and `chat_id`, a run where every posting failed sends a Telegram
    error notice (D-83) instead of failing silently. With an enabled `triage_cfg`, a cheap
    triage pass runs first and only `triaged` postings are scored (D-196).
    """
    sc = scoring_cfg or ScoringConfig()
    triage_failed = False
    triage_on = triage_cfg is not None and triage_cfg.enabled
    if triage_on:
        assert triage_cfg is not None
        tstats = await triage_pending(conn, client, clock, triage_cfg, bot, chat_id)
        log.info("triage: %s", tstats)
        triage_failed = (
            tstats["errors"] > 0
            and tstats["yes"] + tstats["no"] + tstats["cached"] + tstats["exempt"] == 0
        )
    post = apply_post_triage(conn, cues or load_cues(), clock, include_new=not triage_on)
    log.info("post-triage: %s", post)
    scorer = Scorer(
        client,
        model,
        conn,
        clock,
        max_calls,
        budget=LlmBudget(conn, clock, sc.daily_call_cap, ("score", "score_baseline")),
        baseline_budget=LlmBudget(conn, clock, sc.baseline_cap, ("score_baseline",)),
        triage=triage_on,
    )
    stats = await scorer.score_pending(limit if limit is not None else max_calls)
    if bot is not None and chat_id is not None:
        await report_stage_outcome(
            "scoring", bot, chat_id, conn, clock, stats, scorer.last_error, model, sc.daily_call_cap
        )
        from src.sources.hh import notify_auth_error_once

        await notify_auth_error_once(bot, chat_id, conn, clock)  # FC-25, D-219
    if triage_failed and stats["scored"] == 0:
        stats["errors"] += tstats["errors"]  # total triage failure counts for the exit code
    return stats
