"""Polite HTTP client (D-08): honest UA, per-host rate limit, retries with backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

RETRY_AFTER_CAP_S = 60.0
ROBOTS_TTL = timedelta(hours=24)
ROBOTS_AGENT = "oppo-tg"
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
        raw: bool = False,
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
                    if resp.status_code >= 400 and not raw:
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

    async def _robots_allowed(self, url: str) -> bool:
        """robots.txt check, cached 24h per host. 4xx -> allowed; 5xx/failure -> denied."""
        parts = urlsplit(url)
        host = parts.netloc.lower()
        now = self.clock.now()
        row = self.db.execute("SELECT * FROM robots_cache WHERE host=?", (host,)).fetchone()
        if row is not None and now - datetime.fromisoformat(row["fetched_at"]) < ROBOTS_TTL:
            body, allowed_all = row["body"], bool(row["allowed_all"])
        else:
            resp = await self._fetch(f"{parts.scheme}://{host}/robots.txt", None, None, raw=True)
            if resp is None or resp.status_code >= 500:
                log.warning("robots.txt unavailable for %s; skipping", host)
                return False
            if resp.status_code >= 400:
                body, allowed_all = "", True
            else:
                body, allowed_all = resp.text, False
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO robots_cache(host, body, allowed_all, fetched_at)"
                    " VALUES (?,?,?,?)",
                    (host, body, int(allowed_all), now.isoformat()),
                )
        if allowed_all:
            return True
        rp = RobotFileParser()
        rp.parse(body.splitlines())
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return rp.can_fetch(ROBOTS_AGENT, target)

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ttl_s: float | None = None,
        check_robots: bool = True,
    ) -> str | None:
        # Without a db (and clock) there is no robots/response cache (see D-34).
        if self.db is None or self.clock is None:
            resp = await self._fetch(url, params, headers)
            return None if resp is None else resp.text
        cache_key = str(httpx.URL(url, params=params))
        now = self.clock.now()
        cached = None
        if ttl_s is not None:
            cached = self.db.execute(
                "SELECT * FROM http_cache WHERE url=?", (cache_key,)
            ).fetchone()
            if (
                cached is not None
                and cached["status"] == 200
                and (now - datetime.fromisoformat(cached["fetched_at"])).total_seconds() < ttl_s
            ):
                return cached["body"]
        if check_robots and not await self._robots_allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        req_headers = dict(headers or {})
        if cached is not None and cached["status"] == 200:
            if cached["etag"]:
                req_headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                req_headers["If-Modified-Since"] = cached["last_modified"]
        resp = await self._fetch(url, params, req_headers or None)
        if resp is None:
            return None
        if ttl_s is None:
            return resp.text
        if resp.status_code == 304 and cached is not None:
            body = cached["body"]
            etag, lm = cached["etag"], cached["last_modified"]
        else:
            body = resp.text
            etag, lm = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO http_cache(url, status, etag, last_modified, body,"
                " fetched_at) VALUES (?,?,?,?,?,?)",
                (cache_key, 200, etag, lm, body, now.isoformat()),
            )
        return body

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ttl_s: float | None = None,
        check_robots: bool = True,
    ) -> Any | None:
        text = await self.get_text(
            url, params=params, headers=headers, ttl_s=ttl_s, check_robots=check_robots
        )
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            log.warning("invalid JSON from %s", url)
            return None


def build_client(
    db: Any,
    clock: Any,
    *,
    contact: str = "https://github.com/oppo-tg",
    transport: httpx.AsyncBaseTransport | None = None,
    **kwargs: Any,
) -> PoliteClient:
    """Construct a PoliteClient with robots checking and caching active (D-34).

    db and clock are required positional arguments so they cannot be forgotten.
    """
    if db is None or clock is None:
        raise ValueError("build_client requires db and clock")
    return PoliteClient(db=db, clock=clock, contact=contact, transport=transport, **kwargs)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None
