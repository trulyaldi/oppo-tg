"""Polite HTTP client (D-08): honest UA, per-host rate limit, retries with backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

RETRY_AFTER_CAP_S = 60.0
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))

SleepFn = Callable[[float], Awaitable[None]]


class PoliteClient:
    def __init__(
        self,
        db: Any = None,
        clock: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str | None = None,
        contact: str = "https://github.com/oppo-tg",
        min_interval_s: float = 2.0,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        sleep: SleepFn = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.clock = clock
        self.user_agent = user_agent or (f"oppo-tg/0.1 (personal job-alert bot; +{contact})")
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self._sleep = sleep
        self._monotonic = monotonic
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_interval_s - (self._monotonic() - last)
            if wait > 0:
                await self._sleep(wait)

    async def _fetch(
        self,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response | None:
        """GET with rate limiting and retries. None on failure; never raises."""
        host = urlsplit(url).netloc.lower()
        lock = self._locks.setdefault(host, asyncio.Lock())
        for attempt in range(self.max_retries + 1):
            retry_after: float | None = None
            async with lock:
                await self._throttle(host)
                try:
                    resp = await self._client.get(url, params=params, headers=headers)
                except httpx.HTTPError as exc:
                    log.warning("request error for %s: %r", url, exc)
                    resp = None
                finally:
                    self._last_request[host] = self._monotonic()
            if resp is not None:
                if resp.status_code not in RETRY_STATUSES:
                    if resp.status_code >= 400:
                        log.info("HTTP %s for %s", resp.status_code, url)
                        return None
                    return resp
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if attempt >= self.max_retries:
                break
            delay = (
                min(retry_after, RETRY_AFTER_CAP_S)
                if retry_after is not None
                else float(2**attempt)
            )
            await self._sleep(delay)
        log.warning("giving up on %s after %d retries", url, self.max_retries)
        return None

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        resp = await self._fetch(url, params, headers)
        return None if resp is None else resp.text

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any | None:
        text = await self.get_text(url, params=params, headers=headers)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            log.warning("invalid JSON from %s", url)
            return None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None
