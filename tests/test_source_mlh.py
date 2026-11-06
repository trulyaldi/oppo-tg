import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.base import REGISTRY, SourceContext, run_source
from src.sources.mlh import MLHSource, parse_events, season_url

FX = Path(__file__).parent / "fixtures"
BODY = (FX / "mlh_events.html").read_text()
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict({"sources": {"mlh": {"enabled": True}}})


def _ctx(api, now=datetime(2026, 11, 6, 12, tzinfo=UTC)):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(now)
    requests = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /account/\nAllow: /\n")
        requests.append(req)
        return api(req)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=CFG, clock=clock, log=logging.getLogger("t")
    )
    return ctx, requests


async def test_fixture_parses():
    ctx, requests = _ctx(lambda r: httpx.Response(200, text=BODY))
    res = await run_source(MLHSource(), ctx)
    assert res.ok and len(res.items) == 4  # the Oct 9-11 event already ended
    assert str(requests[0].url) == "https://mlh.io/seasons/2027/events"
    first = res.items[0]
    assert first.title == "Solana Hackathon"
    assert first.kind is Kind.HACKATHON
    assert first.deadline == date(2026, 11, 6)
    assert first.remote is True
    assert first.location == "Everywhere, Worldwide, US"
    assert first.url == "https://events.mlh.com/events/15200-solana-hackathon"
    assert "Digital" in first.raw_tags
    assert all(p.deadline is not None for p in res.items)
    in_person = [p for p in res.items if "In-Person" in p.raw_tags]
    assert in_person and all(p.remote is False for p in in_person)
    assert any("Hardware" in p.raw_tags for p in res.items)


def test_ended_events_kept_without_today():
    assert len(parse_events(BODY)) == 5
    assert len(parse_events(BODY, date(2026, 11, 6))) == 4


def test_season_url_from_clock():
    assert season_url(date(2026, 11, 6)) == "https://mlh.io/seasons/2027/events"
    assert season_url(date(2027, 3, 1)) == "https://mlh.io/seasons/2027/events"
    assert season_url(date(2027, 7, 1)) == "https://mlh.io/seasons/2028/events"
    assert season_url(date(2026, 6, 30)) == "https://mlh.io/seasons/2026/events"


async def test_season_url_requested_in_spring():
    ctx, requests = _ctx(
        lambda r: httpx.Response(200, text=BODY), now=datetime(2027, 3, 1, 12, tzinfo=UTC)
    )
    await run_source(MLHSource(), ctx)
    assert str(requests[0].url) == "https://mlh.io/seasons/2027/events"


async def test_no_cards_warns_and_returns_empty(caplog):
    for resp in (
        httpx.Response(200, text="<html><body><main>redesigned</main></body></html>"),
        httpx.Response(200, text=""),
        httpx.Response(200, text="\x00not html{{"),
    ):
        ctx, _ = _ctx(lambda r, x=resp: x)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="t"):
            res = await run_source(MLHSource(), ctx)
        assert res.ok and res.items == []
        assert any("no event cards" in r.message for r in caplog.records)


async def test_http_errors_return_empty():
    for status in (403, 404, 429, 500):
        ctx, _ = _ctx(lambda r, s=status: httpx.Response(s, text="nope"))
        res = await run_source(MLHSource(), ctx)
        assert res.ok and res.items == []


async def test_robots_disallow_skips_fetch():
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 6, tzinfo=UTC))
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /seasons/\n")
        seen.append(req)
        return httpx.Response(200, text=BODY)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=CFG, clock=clock, log=logging.getLogger("t")
    )
    res = await run_source(MLHSource(), ctx)
    assert res.items == [] and seen == []


def test_registered_and_enabled():
    assert "mlh" in REGISTRY
    src = REGISTRY["mlh"]()
    assert src.enabled(Settings.from_env(ENV), CFG)
    assert not src.enabled(Settings.from_env(ENV), AppConfig.from_dict({}))
