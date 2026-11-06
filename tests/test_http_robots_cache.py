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


def _redir_env(b_robots, loop=False):
    def a_x(req):
        return httpx.Response(302, headers={"Location": "https://b.test/y"})

    handler_state = {}

    def handler(req):
        handler_state.setdefault("reqs", []).append(f"{req.url.host}{req.url.path}")
        h, p = req.url.host, req.url.path
        if p == "/robots.txt":
            return b_robots if h == "b.test" else httpx.Response(404)
        if loop:
            return httpx.Response(302, headers={"Location": f"/loop{len(handler_state['reqs'])}"})
        if h == "a.test" and p == "/x":
            return a_x(req)
        if h == "a.test" and p == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        if h == "a.test" and p == "/new":
            return httpx.Response(200, text="new")
        if h == "b.test" and p == "/y":
            return httpx.Response(200, text="Y")
        return httpx.Response(404)

    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    sleeps = []

    async def sleep(d):
        sleeps.append(d)

    client = PoliteClient(
        db=conn,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        sleep=sleep,
    )
    return client, conn, handler_state


async def test_redirect_to_disallowed_host_refused():
    c, _, st = _redir_env(httpx.Response(200, text="User-agent: *\nDisallow: /y\n"))
    assert await c.get_text("https://a.test/x") is None
    assert "b.test/y" not in st["reqs"]
    assert "b.test/robots.txt" in st["reqs"]


async def test_redirect_to_allowed_host_returns_body_and_caches_robots():
    c, conn, _ = _redir_env(httpx.Response(200, text="User-agent: *\nAllow: /\n"))
    assert await c.get_text("https://a.test/x") == "Y"
    hosts = {r["host"] for r in conn.execute("SELECT host FROM robots_cache")}
    assert hosts == {"a.test", "b.test"}


async def test_same_host_redirect_no_extra_robots_fetch():
    c, _, st = _redir_env(httpx.Response(404))
    assert await c.get_text("https://a.test/old") == "new"
    assert st["reqs"].count("a.test/robots.txt") == 1


async def test_redirect_loop_stops_after_five_hops():
    c, _, st = _redir_env(httpx.Response(404), loop=True)
    assert await c.get_text("https://a.test/x") is None
    non_robots = [r for r in st["reqs"] if not r.endswith("/robots.txt")]
    assert len(non_robots) == 6  # original + 5 hops


# ---- FIX-12: redirect hardening ----

SECRET = {"Authorization": "Bearer T", "HH-User-Agent": "u"}


def _hop_client(routes, db=True):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        key = f"{req.url.scheme}://{req.url.host}{req.url.path}"
        r = routes.get(key)
        return r if r is not None else httpx.Response(404)

    async def sleep(d):
        pass

    conn = None
    clock = None
    if db:
        conn = dbm.connect(":memory:")
        dbm.migrate(conn)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    c = PoliteClient(
        db=conn,
        clock=clock,
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        sleep=sleep,
    )
    return c, conn, seen


def _redir(loc, code=301):
    return httpx.Response(code, headers={"Location": loc})


async def test_cross_host_hop_strips_caller_headers():
    c, _, seen = _hop_client(
        {
            "https://a.test/x": _redir("https://b.test/y"),
            "https://b.test/y": httpx.Response(200, text="Y"),
        }
    )
    assert await c.get_text("https://a.test/x", headers=SECRET, check_robots=False) == "Y"
    last = seen[-1]
    assert last.url.host == "b.test"
    assert "authorization" not in last.headers
    assert "hh-user-agent" not in last.headers
    assert last.headers["user-agent"] == c.user_agent
    assert seen[0].headers["authorization"] == "Bearer T"


async def test_same_origin_hop_forwards_headers():
    c, _, seen = _hop_client(
        {"https://a.test/x": _redir("/z"), "https://a.test/z": httpx.Response(200, text="Z")}
    )
    assert await c.get_text("https://a.test/x", headers=SECRET, check_robots=False) == "Z"
    assert seen[-1].headers["authorization"] == "Bearer T"
    assert seen[-1].headers["hh-user-agent"] == "u"


async def test_different_port_hop_strips_headers():
    c, _, seen = _hop_client(
        {
            "https://a.test/x": _redir("https://a.test:8443/z"),
            "https://a.test/z": httpx.Response(200, text="Z"),
        }
    )
    await c.get_text("https://a.test/x", headers=SECRET, check_robots=False)
    assert seen[-1].url.port == 8443
    assert "authorization" not in seen[-1].headers


async def test_https_to_http_downgrade_refused():
    c, _, seen = _hop_client(
        {
            "https://a.test/down": _redir("http://a.test/z"),
            "http://a.test/z": httpx.Response(200, text="Z"),
        }
    )
    assert await c.get_text("https://a.test/down", headers=SECRET, check_robots=False) is None
    assert all(r.url.scheme == "https" for r in seen)


async def test_http_to_https_upgrade_allowed():
    c, _, seen = _hop_client(
        {
            "http://a.test/x": _redir("https://a.test/x"),
            "https://a.test/x": httpx.Response(200, text="S"),
        }
    )
    assert await c.get_text("http://a.test/x", headers=SECRET, check_robots=False) == "S"
    assert seen[-1].url.scheme == "https"


async def test_3xx_without_location_is_failure_and_not_cached():
    c, conn, _ = _hop_client({"https://a.test/noloc": httpx.Response(302, text="moved")})
    assert await c.get_text("https://a.test/noloc", ttl_s=3600, check_robots=False) is None
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


async def test_304_without_cache_is_failure():
    c, conn, _ = _hop_client({"https://a.test/n": httpx.Response(304)})
    assert await c.get_text("https://a.test/n", ttl_s=3600, check_robots=False) is None
    assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0


def test_default_contact_is_project_repo():
    from src.http import build_client

    assert "https://github.com/trulyaldi/oppo-tg" in PoliteClient().user_agent
    conn = dbm.connect(":memory:")
    c = build_client(conn, FixedClock(datetime(2026, 1, 1, tzinfo=UTC)))
    assert "https://github.com/trulyaldi/oppo-tg" in c.user_agent
