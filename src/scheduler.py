"""APScheduler wiring for unattended mode (D-20, D-67). All work is injected (D-05)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.clock import Clock
from src.config import SchedulerConfig

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]


TierRunner = Callable[[str], Awaitable[object]]
TIER_STAGGER_MINUTES = 2  # first runs of tier1/2/3 start 0, 2 and 4 minutes after start


def default_tiers() -> dict[str, tuple[int, int]]:
    """tier -> (minutes, jitter seconds), from the SchedulerConfig defaults."""
    c = SchedulerConfig()
    return {
        "tier1": (c.tier1_minutes, c.tier1_jitter_s),
        "tier2": (c.tier2_minutes, c.tier2_jitter_s),
        "tier3": (c.tier3_minutes, c.tier3_jitter_s),
    }


def tiers_from_config(c: SchedulerConfig) -> dict[str, tuple[int, int]]:
    return {
        "tier1": (c.tier1_minutes, c.tier1_jitter_s),
        "tier2": (c.tier2_minutes, c.tier2_jitter_s),
        "tier3": (c.tier3_minutes, c.tier3_jitter_s),
    }


@dataclass
class SchedulerDeps:
    clock: Clock
    run_tier: TierRunner
    digest: Job
    prune: Job
    tiers: dict[str, tuple[int, int]] = field(default_factory=default_tiers)
    timezone: str = "Asia/Almaty"
    digest_time: str = "09:00"
    discover: Job | None = None
    discover_time: str = "05:30"


def safe_job(name: str, fn: Job) -> Callable[[], Awaitable[None]]:
    """Wrap a job so any exception is logged and never reaches the scheduler."""

    async def wrapper() -> None:
        try:
            await fn()
        except Exception:
            log.exception("job %s failed", name)

    wrapper.__name__ = name
    return wrapper


def parse_hhmm(value: str) -> tuple[int, int]:
    h, m = value.strip().split(":")
    return int(h), int(m)


def build_scheduler(deps: SchedulerDeps) -> AsyncIOScheduler:
    tz = ZoneInfo(deps.timezone)
    sched = AsyncIOScheduler(timezone=tz)

    hour, minute = parse_hhmm(deps.digest_time)
    common = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    start = deps.clock.now()
    for i, (tier, (minutes, jitter)) in enumerate(deps.tiers.items()):

        async def run(tier: str = tier) -> None:
            await deps.run_tier(tier)

        sched.add_job(
            safe_job(f"tier_{tier}", run),
            IntervalTrigger(minutes=max(1, minutes), jitter=jitter, timezone=tz),
            id=f"collect_{tier}",
            next_run_time=start + timedelta(minutes=TIER_STAGGER_MINUTES * i),
            **common,
        )
    sched.add_job(
        safe_job("daily_digest", deps.digest),
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_digest",
        **common,
    )
    sched.add_job(
        safe_job("prune", deps.prune),
        CronTrigger(hour=4, minute=0, timezone=tz),
        id="prune",
        **common,
    )
    if deps.discover is not None:
        d_hour, d_minute = parse_hhmm(deps.discover_time)
        sched.add_job(
            safe_job("board_discovery", deps.discover),
            CronTrigger(hour=d_hour, minute=d_minute, timezone=tz),
            id="board_discovery",
            **common,
        )
    return sched
