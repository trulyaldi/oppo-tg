"""Per-host politeness: spacing with jitter, global concurrency, persistent backoff (D-187)."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from src.config import PolitenessConfig

log = logging.getLogger(__name__)

# Statuses that count as a failed run for backoff purposes (429, 5xx, 403).
_FAIL_STATUSES = frozenset({403, 429}) | frozenset(range(500, 600))


class HostLimiter:
    def __init__(
        self,
        config: PolitenessConfig | None = None,
        db: Any = None,
        clock: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        default_interval_s: float | None = None,
    ) -> None:
        self.config = config or PolitenessConfig()
        self._default_interval = (
            self.config.default_min_interval_s if default_interval_s is None else default_interval_s
        )
        self.db = db
        self.clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._rand = rand
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._sem = asyncio.Semaphore(self.config.max_concurrent_requests)
        # Without a db, backoff lives in memory only: host -> (failures, until).
        self._mem: dict[str, tuple[int, datetime | None]] = {}
        self._logged: set[str] = set()
        self._hosts = {h.lower().strip("."): s for h, s in self.config.hosts.items()}

    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    def interval_for(self, host: str) -> float:
        """Longest-suffix rule: `a.jobs.example.com` uses the `jobs.example.com` entry."""
        host = host.lower()
        best: str | None = None
        for key in self._hosts:
            if (host == key or host.endswith("." + key)) and (best is None or len(key) > len(best)):
                best = key
        return self._hosts[best] if best is not None else self._default_interval

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        """Hold the host lock, sleep for the host's spacing, then take a global slot."""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                gap = self.interval_for(host) * (1 + self._rand() * self.config.jitter_fraction)
                wait = gap - (self._monotonic() - last)
                if wait > 0:
                    await self._sleep(wait)
            # The global slot is taken after the spacing sleep (D-271), so a host waiting out
            # its spacing does not block other hosts.
            async with self._sem:
                try:
                    yield
                finally:
                    self._last[host] = self._monotonic()

    def _read(self, host: str) -> tuple[int, datetime | None]:
        if self.db is None:
            return self._mem.get(host, (0, None))
        row = self.db.execute(
            "SELECT failures, backoff_until FROM host_state WHERE host=?", (host,)
        ).fetchone()
        if row is None:
            return 0, None
        until = datetime.fromisoformat(row["backoff_until"]) if row["backoff_until"] else None
        return row["failures"], until

    def backoff_until(self, host: str) -> datetime | None:
        until = self._read(host.lower())[1]
        return until if until is not None and until > self._now() else None

    def in_backoff(self, host: str) -> bool:
        return self.backoff_until(host) is not None

    def new_run(self) -> None:
        """Reset the log-once-per-host-per-run memory."""
        self._logged.clear()

    def skip_logged(self, host: str) -> None:
        if host not in self._logged:
            self._logged.add(host)
            log.info("host %s is in backoff until %s; skipping", host, self.backoff_until(host))

    def record(
        self,
        host: str,
        outcome: int | BaseException,
        retry_after: float | None = None,
    ) -> None:
        """Record the final outcome of one request run (after retries) for a host."""
        host = host.lower()
        now = self._now()
        status = outcome if isinstance(outcome, int) else None
        failures, until = self._read(host)
        if status is not None and status < 400:
            failures, until = 0, None
        elif status is None or status in _FAIL_STATUSES:
            failures += 1
            delay = min(self.config.backoff_base_s * 2 ** (failures - 1), self.config.backoff_max_s)
            if retry_after is not None:
                delay = min(max(delay, retry_after), self.config.backoff_max_s)
            until = now + timedelta(seconds=delay)
        # else: a neutral 4xx (404 etc.) leaves the failure count alone.
        if self.db is None:
            self._mem[host] = (failures, until)
            return
        with self.db:
            self.db.execute(
                "INSERT INTO host_state(host, failures, backoff_until, last_status, updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET failures=excluded.failures,"
                " backoff_until=excluded.backoff_until, last_status=excluded.last_status,"
                " updated_at=excluded.updated_at",
                (host, failures, until.isoformat() if until else None, status, now.isoformat()),
            )
