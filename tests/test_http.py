import httpx

from src.http import PoliteClient


class Env:
    def __init__(self, responses, **kw):
        self.t = 0.0
        self.sleeps: list[float] = []
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

        def handler(req: httpx.Request) -> httpx.Response:
            self.requests.append(req)
            r = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
            return r if isinstance(r, httpx.Response) else httpx.Response(r)

        async def sleep(d: float) -> None:
            self.sleeps.append(d)
            self.t += d

        kw.setdefault("min_interval_s", 0.0)
        self.client = PoliteClient(
            transport=httpx.MockTransport(handler),
            contact="me@example.com",
            sleep=sleep,
            monotonic=lambda: self.t,
            **kw,
        )


async def test_user_agent():
    e = Env([200])
    await e.client.get_text("https://x.test/a")
    ua = e.requests[0].headers["User-Agent"]
    assert ua.startswith("oppo-tg/") and "me@example.com" in ua


async def test_retry_5xx_then_ok():
    e = Env([503, 503, httpx.Response(200, text="ok")])
    assert await e.client.get_text("https://x.test/a") == "ok"
    assert e.sleeps == [1, 2]


async def test_retry_after_and_cap():
    e = Env([httpx.Response(429, headers={"Retry-After": "7"}), 200])
    assert await e.client.get_text("https://x.test/a") == ""
    assert e.sleeps == [7]
    e = Env([httpx.Response(429, headers={"Retry-After": "999"}), 200])
    assert await e.client.get_text("https://x.test/a") is None
    assert e.sleeps == []  # above the cap: backoff recorded (FC-07), not slept


async def test_404_no_retry():
    e = Env([404])
    assert await e.client.get_text("https://x.test/a") is None
    assert len(e.requests) == 1 and e.sleeps == []


async def test_persistent_500_returns_none():
    e = Env([500], max_retries=3)
    assert await e.client.get_text("https://x.test/a") is None
    assert len(e.requests) == 4
    assert e.sleeps == [1, 2, 4]


async def test_timeout_retried():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, text="ok")

    sleeps = []

    async def sleep(d):
        sleeps.append(d)

    c = PoliteClient(transport=httpx.MockTransport(handler), min_interval_s=0, sleep=sleep)
    assert await c.get_text("https://x.test/a") == "ok"
    assert sleeps == [1]


async def test_rate_limit_same_host():
    e = Env([200], min_interval_s=2.0)
    await e.client.get_text("https://x.test/a")
    await e.client.get_text("https://x.test/b")
    assert len(e.sleeps) == 1 and e.sleeps[0] >= 2.0
    await e.client.get_text("https://other.test/c")
    assert len(e.sleeps) == 1


async def test_get_json():
    e = Env([httpx.Response(200, json={"a": 1})])
    assert await e.client.get_json("https://x.test/a") == {"a": 1}


def test_parse_retry_after_forms():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from src.http import _parse_retry_after

    now = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    assert _parse_retry_after("7", now) == 7.0
    assert _parse_retry_after(format_datetime(now + timedelta(hours=2), usegmt=True), now) == 7200
    assert _parse_retry_after(format_datetime(now - timedelta(hours=1), usegmt=True), now) == 0.0
    assert _parse_retry_after("garbage", now) is None
    assert _parse_retry_after(None, now) is None
