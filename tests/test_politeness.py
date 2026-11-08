"""HostLimiter and PoliteClient politeness (D-187), with fake clock, sleep and rand."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import PolitenessConfig
from src.http import PoliteClient
from src.politeness import HostLimiter

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


class Env:
    def __init__(self, handler, cfg=None, rand=0.5, **kw):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.calls = 0
        self.conn = dbm.connect(":memory:")
        dbm.migrate(self.conn)
        self.clock = FixedClock(T0)

        async def sleep(d):
            self.sleeps.append(d)
            self.t += d

        def h(req):
            self.calls += 1
            return handler(req)

        self.client = PoliteClient(
            db=self.conn,
            clock=self.clock,
            transport=httpx.MockTransport(h),
            politeness=cfg or PolitenessConfig(),
            sleep=sleep,
            monotonic=lambda: self.t,
            rand=lambda: rand,
            **{"max_retries": 0, **kw},
        )

    def until(self, host):
        row = self.conn.execute("SELECT * FROM host_state WHERE host=?", (host,)).fetchone()
        return row


async def test_spacing_with_jitter():
    e = Env(lambda r: httpx.Response(200, text="ok"))
    await e.client.get_text("https://x.test/a", check_robots=False)
    await e.client.get_text("https://x.test/b", check_robots=False)
    assert e.sleeps == [2.0 * 1.125]


def test_longest_suffix_rule():
    cfg = PolitenessConfig(hosts={"example.com": 9.0, "jobs.example.com": 5.0})
    lim = HostLimiter(cfg)
    assert lim.interval_for("a.jobs.example.com") == 5.0
    assert lim.interval_for("jobs.example.com") == 5.0
    assert lim.interval_for("www.example.com") == 9.0
    assert lim.interval_for("notexample.com") == cfg.default_min_interval_s


async def test_global_concurrency_cap():
    state = {"now": 0, "max": 0}

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            state["now"] -= 1
            return httpx.Response(200, text="ok")

    c = PoliteClient(
        transport=T(),
        politeness=PolitenessConfig(max_concurrent_requests=3),
        sleep=lambda d: asyncio.sleep(0),
    )
    urls = [f"https://h{i}.test/" for i in range(10)]
    res = await asyncio.gather(*(c.get_text(u, check_robots=False) for u in urls))
    assert all(r == "ok" for r in res)
    assert state["max"] == 3


async def test_backoff_doubles_caps_and_success_resets():
    status = {"v": 429}
    cfg = PolitenessConfig(backoff_max_s=1000)
    e = Env(lambda r: httpx.Response(status["v"], text="x"), cfg)
    for _ in range(4):
        assert await e.client.get_text("https://x.test/a", check_robots=False) is None
        until = datetime.fromisoformat(e.until("x.test")["backoff_until"])
        e.clock.set(until + timedelta(seconds=1))
    row = e.until("x.test")
    assert row["failures"] == 4
    status["v"] = 200
    e.clock.set(datetime.fromisoformat(row["backoff_until"]) + timedelta(seconds=1))
    assert await e.client.get_text("https://x.test/a", check_robots=False) == "x"
    row = e.until("x.test")
    assert row["failures"] == 0 and row["backoff_until"] is None


async def test_backoff_values_300_600_1200():
    e = Env(lambda r: httpx.Response(429), PolitenessConfig())
    seen = []
    for _ in range(3):
        await e.client.get_text("https://x.test/a", check_robots=False)
        now = e.clock.now()
        until = datetime.fromisoformat(e.until("x.test")["backoff_until"])
        seen.append((until - now).total_seconds())
        e.clock.set(until + timedelta(seconds=1))
    assert seen == [300, 600, 1200]


async def test_backoff_capped():
    cfg = PolitenessConfig(backoff_base_s=300, backoff_max_s=500)
    e = Env(lambda r: httpx.Response(503), cfg)
    for _ in range(3):
        await e.client.get_text("https://x.test/a", check_robots=False)
        e.clock.set(datetime.fromisoformat(e.until("x.test")["backoff_until"]))
        e.clock.set(e.clock.now() + timedelta(seconds=1))
    await e.client.get_text("https://x.test/a", check_robots=False)
    assert (
        datetime.fromisoformat(e.until("x.test")["backoff_until"]) - e.clock.now()
    ).total_seconds() == 500


async def test_in_backoff_makes_no_calls():
    e = Env(lambda r: httpx.Response(429))
    await e.client.get_text("https://x.test/a", check_robots=False)
    assert e.calls == 1 and e.client.limiter.in_backoff("x.test")
    assert await e.client.get_text("https://x.test/b", check_robots=False) is None
    assert await e.client.get_text("https://x.test/c", check_robots=False) is None
    assert e.calls == 1
    assert await e.client.get_text("https://y.test/a", check_robots=False) is None
    assert e.calls == 2


async def test_backoff_logged_once_per_run(caplog):
    e = Env(lambda r: httpx.Response(429))
    await e.client.get_text("https://x.test/a", check_robots=False)
    with caplog.at_level("INFO", logger="src.politeness"):
        await e.client.get_text("https://x.test/b", check_robots=False)
        await e.client.get_text("https://x.test/c", check_robots=False)
        assert sum("backoff" in r.message for r in caplog.records) == 1
        e.client.limiter.new_run()
        await e.client.get_text("https://x.test/d", check_robots=False)
        assert sum("backoff" in r.message for r in caplog.records) == 2


async def test_long_retry_after_sets_backoff_without_sleeping():
    e = Env(lambda r: httpx.Response(429, headers={"Retry-After": "3600"}), max_retries=3)
    assert await e.client.get_text("https://x.test/a", check_robots=False) is None
    assert e.sleeps == [] and e.calls == 1
    until = datetime.fromisoformat(e.until("x.test")["backoff_until"])
    assert (until - T0).total_seconds() == 3600


async def test_timeout_records_backoff():
    def boom(req):
        raise httpx.ReadTimeout("slow", request=req)

    e = Env(boom)
    assert await e.client.get_text("https://x.test/a", check_robots=False) is None
    row = e.until("x.test")
    assert row["failures"] == 1 and row["last_status"] is None and row["backoff_until"]


async def test_404_is_neutral():
    e = Env(lambda r: httpx.Response(404))
    await e.client.get_text("https://x.test/a", check_robots=False)
    assert not e.client.limiter.in_backoff("x.test")
    assert e.until("x.test")["failures"] == 0


async def test_http_date_retry_after_sets_backoff_without_sleeping():
    from email.utils import format_datetime

    hdr = format_datetime(T0 + timedelta(hours=2), usegmt=True)
    e = Env(lambda r: httpx.Response(429, headers={"Retry-After": hdr}), max_retries=3)
    assert await e.client.get_text("https://x.test/a", check_robots=False) is None
    assert e.sleeps == [] and e.calls == 1
    until = datetime.fromisoformat(e.until("x.test")["backoff_until"])
    assert (until - T0).total_seconds() == 7200


async def test_spacing_sleep_does_not_hold_global_slot():
    t = {"v": 0.0}
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def sleep(d):
        sleeping.set()
        await release.wait()

    lim = HostLimiter(
        PolitenessConfig(max_concurrent_requests=1, jitter_fraction=0.1),
        monotonic=lambda: t["v"],
        sleep=sleep,
        default_interval_s=5.0,
    )
    async with lim.slot("a.test"):
        pass
    done_b = []

    async def a():
        async with lim.slot("a.test"):  # must sleep for spacing
            pass

    async def b():
        async with lim.slot("b.test"):
            done_b.append(1)

    ta = asyncio.create_task(a())
    await sleeping.wait()
    await asyncio.wait_for(b(), timeout=1)  # not blocked by A's sleep
    assert done_b == [1]
    release.set()
    await ta
