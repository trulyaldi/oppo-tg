from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.http import PoliteClient

ROBOTS = (Path(__file__).parent / "fixtures" / "robots_disallow_jobs.txt").read_text()


class Env:
    def __init__(self, routes):
        self.requests: list[httpx.Request] = []
        self.routes = routes
        self.clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
        self.conn = dbm.connect(":memory:")
        dbm.migrate(self.conn)

        def handler(req):
            self.requests.append(req)
            r = self.routes[req.url.path]
            if callable(r):
                r = r(req)
            return r if isinstance(r, httpx.Response) else httpx.Response(r)

        async def sleep(d):
            pass

        self.client = PoliteClient(
            db=self.conn,
            clock=self.clock,
            transport=httpx.MockTransport(handler),
            min_interval_s=0,
            sleep=sleep,
        )

    def paths(self):
        return [r.url.path for r in self.requests]


async def test_robots_disallow_and_allow():
    e = Env(
        {"/robots.txt": httpx.Response(200, text=ROBOTS), "/about": httpx.Response(200, text="a")}
    )
    assert await e.client.get_text("https://x.test/jobs/1") is None
    assert "/jobs/1" not in e.paths()
    assert await e.client.get_text("https://x.test/about") == "a"
    assert e.paths().count("/robots.txt") == 1


async def test_robots_404_allowed():
    e = Env({"/robots.txt": 404, "/jobs/1": httpx.Response(200, text="j")})
    assert await e.client.get_text("https://x.test/jobs/1") == "j"


async def test_robots_500_disallowed_not_cached():
    e = Env({"/robots.txt": 500, "/a": httpx.Response(200, text="a")})
    e.client.max_retries = 0
    assert await e.client.get_text("https://x.test/a") is None
    assert "/a" not in e.paths()
    assert e.conn.execute("SELECT COUNT(*) FROM robots_cache").fetchone()[0] == 0


async def test_check_robots_false_skips():
    e = Env({"/jobs/1": httpx.Response(200, text="j")})
    assert await e.client.get_text("https://x.test/jobs/1", check_robots=False) == "j"
    assert e.paths() == ["/jobs/1"]


async def test_robots_refetched_after_24h():
    e = Env({"/robots.txt": httpx.Response(200, text=ROBOTS), "/a": httpx.Response(200, text="a")})
    await e.client.get_text("https://x.test/a")
    e.clock.set(e.clock.now() + timedelta(hours=25))
    await e.client.get_text("https://x.test/a")
    assert e.paths().count("/robots.txt") == 2


async def test_cache_ttl_and_conditional():
    state = {"n": 0}

    def page(req):
        state["n"] += 1
        if req.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text="body", headers={"ETag": '"v1"'})

    e = Env({"/robots.txt": 404, "/p": page})
    assert await e.client.get_text("https://x.test/p", ttl_s=3600) == "body"
    assert await e.client.get_text("https://x.test/p", ttl_s=3600) == "body"
    assert e.paths().count("/p") == 1
    e.clock.set(e.clock.now() + timedelta(hours=2))
    assert await e.client.get_text("https://x.test/p", ttl_s=3600) == "body"
    assert e.paths().count("/p") == 2
    assert e.requests[-1].headers["If-None-Match"] == '"v1"'
    # fetched_at refreshed by 304 -> within TTL again
    await e.client.get_text("https://x.test/p", ttl_s=3600)
    assert e.paths().count("/p") == 2
