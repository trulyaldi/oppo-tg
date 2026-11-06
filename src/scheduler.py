"""APScheduler wiring for unattended mode (D-20, D-67). All work is injected (D-05)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.clock import Clock

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[object]]


@dataclass
class SchedulerDeps:
    clock: Clock
    collect: Job
    score: Job
    alerts: Job
    digest: Job
    prune: Job
    timezone: str = "Asia/Almaty"
    digest_time: str = "09:00"
    collect_hours: int = 3


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

    async def collect_and_score() -> None:
        # Each step is isolated so a failure still lets later steps run.
        for step_name, step in (
            ("collect", deps.collect),
            ("score", deps.score),
            ("alerts", deps.alerts),
        ):
            try:
                await step()
            except Exception:
                log.exception("collect_and_score step %s failed", step_name)

    hour, minute = parse_hhmm(deps.digest_time)
    common = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    sched.add_job(
        safe_job("collect_and_score", collect_and_score),
        IntervalTrigger(hours=max(1, deps.collect_hours), timezone=tz),
        id="collect_and_score",
        next_run_time=deps.clock.now(),
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
    return sched
