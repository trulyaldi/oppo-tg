from datetime import UTC, datetime

import httpx
import pytest

from src import db as dbm
from src.clock import FixedClock
from src.http import BLOCKED_HOSTS, PoliteClient, is_blocked_host


def make(handler):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)

    async def sleep(d):
        pass

    return PoliteClient(
        db=conn,
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        sleep=sleep,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/x",
        "https://lnkd.in/abc",
        "https://www.ycombinator.com/companies/foo",
        "https://devpost.com/api/hackathons",
    ],
)
async def test_blocked_no_requests(url, caplog):
    caplog.set_level("INFO", logger="src.http")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, text="x")

    c = make(handler)
    assert await c.get_text(url) is None
    assert await c.get_json(url) is None
    assert await c.get_text(url, check_robots=False) is None
    assert seen == []
    assert "blocked host" in caplog.text


async def test_redirect_to_blocked_host():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "https://linkedin.com/in/y"})

    c = make(handler)
    assert await c.get_text("https://a.test/page") is None
    assert await c.get_text("https://a.test/page", check_robots=False) is None
    assert all(h == "a.test" for h in seen)


def test_suffix_matching():
    assert len(BLOCKED_HOSTS) == 16
    assert is_blocked_host("https://uk.linkedin.com/x")
    assert is_blocked_host("https://news.ycombinator.com/item?id=1")
    assert not is_blocked_host("https://notlinkedin.com/")
    assert not is_blocked_host("https://example.com/linkedin.com")
