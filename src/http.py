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

from src.config import DEFAULT_HTTP_CONTACT

log = logging.getLogger(__name__)

RETRY_AFTER_CAP_S = 60.0
ROBOTS_TTL = timedelta(hours=24)
ROBOTS_AGENT = "oppo-tg"
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
RETRY_STATUSES = frozenset({429}) | frozenset(range(500, 600))

# D-104: never fetched, even if a future code path asks. Suffix match on the host.
BLOCKED_HOSTS = (
    "linkedin.com",
    "lnkd.in",
    "ycombinator.com",
    "devpost.com",
    "glassdoor.com",
    "indeed.com",
    "wellfound.com",
    "angel.co",
    "crunchbase.com",
    "hunter.io",
    "apollo.io",
    "rocketreach.co",
    "zoominfo.com",
    "lusha.com",
    "contactout.com",
    "signalhire.com",
)


def is_blocked_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def is_unsafe_host(url: str) -> bool:
    """True for localhost, internal-looking names and IP literals (any form, v4 or v6).

    Posting URLs come from third-party feeds, so they must not steer the bot at
    loopback, private, link-local or cloud-metadata addresses (FIX-24, D-149).
    """
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True
    if not host or host == "localhost" or host.endswith(INTERNAL_SUFFIXES):
        return True
    if ":" in host:  # IPv6 literal (hostname has the brackets stripped)
        return True
    last = host.rsplit(".", 1)[-1]
    return last.isdigit() or last.startswith("0x")  # 1.2.3.4, 2130706433, 0x7f.1


SleepFn = Callable[[float], Awaitable[None]]


class PoliteClient:
    def __init__(
        self,
        db: Any = None,
        clock: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str | None = None,
        contact: str = DEFAULT_HTTP_CONTACT,
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
        # Hosts that answered 401/403/429; callers that must not evade (N6) check this.
        self.denied: set[str] = set()
        self._last_request: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_s,
            headers={"User-Agent": self.user_agent},
            follow_redirects=False,
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
        check_robots: bool = False,
    ) -> httpx.Response | None:
        """GET following redirects manually (FIX-10, D-80). None on failure; never raises.

        Every hop gets the per-host throttle and retries; with check_robots each hop's
        URL is also checked against its host's robots.txt. At most MAX_REDIRECTS hops.
        """
        if is_blocked_host(url):
            log.info("blocked host, not fetching %s", urlsplit(url).hostname)
            return None
        if is_unsafe_host(url):
            log.info("unsafe host (local or IP literal), not fetching")
            return None
        resp = await self._fetch_once(url, params, headers, raw)
        hops = 0
        while resp is not None and _is_redirect(resp):
            hops += 1
            if hops > MAX_REDIRECTS:
                log.warning("too many redirects for %s", url)
                return None
            try:
                nxt = httpx.URL(url).join(resp.headers["location"])
            except (httpx.InvalidURL, ValueError):
                log.warning("bad redirect Location from %s", url)
                return None
            if nxt.scheme not in ("http", "https") or not nxt.host:
                log.warning("refusing redirect to %s", nxt)
                return None
            nxt_url = str(nxt)
            if is_blocked_host(nxt_url):
                log.info("blocked host on redirect, not fetching %s", nxt.host)
                return None
            if is_unsafe_host(nxt_url):
                log.info("unsafe host on redirect, not fetching")
                return None
            if check_robots and not await self._robots_allowed(nxt_url):
                log.info("robots.txt disallows redirect target %s", nxt_url)
                return None
            cur = httpx.URL(url)
            if cur.scheme == "https" and nxt.scheme == "http":
                log.warning("refusing https -> http redirect to %s", nxt_url)
                return None
            # Custom headers (may carry credentials) only on same-origin hops, or the
            # http -> https upgrade of the same host on default ports (FIX-12).
            same_origin = _origin(cur) == _origin(nxt) or (
                cur.scheme == "http"
                and nxt.scheme == "https"
                and (cur.host or "").lower() == (nxt.host or "").lower()
                and _origin(cur)[2] == 80
                and _origin(nxt)[2] == 443
            )
            url = nxt_url
            resp = await self._fetch_once(url, None, headers if same_origin else None, raw)
        if resp is not None and 300 <= resp.status_code < 400 and resp.status_code != 304:
            log.warning("redirect status %s without usable Location from %s", resp.status_code, url)
            return None
        return resp

    async def _fetch_once(
        self,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        raw: bool = False,
    ) -> httpx.Response | None:
        """Single GET with rate limiting and retries (no redirect handling)."""
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
                if resp.status_code in (401, 403, 429):
                    self.denied.add((urlsplit(url).hostname or "").lower())
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
        if is_blocked_host(url):
            log.info("blocked host, not fetching %s", urlsplit(url).hostname)
            return None
        # Without a db (and clock) there is no robots/response cache (see D-34).
        if self.db is None or self.clock is None:
            resp = await self._fetch(url, params, headers)
            if resp is None or resp.status_code == 304:
                return None
            return resp.text
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
        resp = await self._fetch(url, params, req_headers or None, check_robots=check_robots)
        if resp is None:
            return None
        if resp.status_code == 304 and cached is None:
            log.warning("304 without a cached response for %s", url)
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
    contact: str = DEFAULT_HTTP_CONTACT,
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


def _is_redirect(resp: httpx.Response) -> bool:
    # Not resp.is_redirect: httpx counts 304 Not Modified, which must stay a plain response.
    return resp.status_code in REDIRECT_STATUSES and "location" in resp.headers


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    port = url.port or {"http": 80, "https": 443}.get(url.scheme)
    return (url.scheme, (url.host or "").lower(), port)
