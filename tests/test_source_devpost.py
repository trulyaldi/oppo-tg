import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.base import REGISTRY, SourceContext, run_source
from src.sources.devpost import DevpostSource, parse_deadline, parse_page

FX = Path(__file__).parent / "fixtures"
BODY = (FX / "devpost_hackathons.json").read_text()
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict({"sources": {"devpost": {"enabled": True}}})


@pytest.fixture(autouse=True)
def _unblock_devpost(monkeypatch):
    # The source is disabled (D-50) and PoliteClient now blocks devpost.com (D-104);
    # these tests exercise the parser only, so lift the block locally.
    monkeypatch.setattr("src.http.BLOCKED_HOSTS", ())


def _ctx(api):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 6, tzinfo=UTC))
    requests = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        requests.append(req)
        return api(req)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=CFG, clock=clock, log=logging.getLogger("t")
    )
    return ctx, requests


def _paged(req):
    # page 1 has the fixture, later pages are empty
    if req.url.params.get("page") == "1":
        return httpx.Response(200, text=BODY)
    return httpx.Response(200, json={"hackathons": []})


async def test_fixture_parses():
    ctx, requests = _ctx(_paged)
    res = await run_source(DevpostSource(), ctx)
    assert res.ok and len(res.items) == 6
    assert len(requests) == 2  # page 2 empty -> stop
    assert "status%5B%5D=upcoming" in str(requests[0].url) or "status[]=upcoming" in str(
        requests[0].url
    )
    first = res.items[0]
    assert first.title == "PayPal AI Hackathon"
    assert first.company == "PayPal"
    assert first.kind is Kind.HACKATHON
    assert first.deadline == date(2026, 11, 12)
    assert first.remote is True
    assert "Machine Learning/AI" in first.raw_tags
    assert "prize $67,500" in first.raw_tags
    berlin = res.items[4]
    assert berlin.remote is None and berlin.location == "Berlin, Germany"
    assert berlin.deadline == date(2027, 1, 20)


def test_deadline_formats():
    assert parse_deadline("Oct 01 - Nov 30, 2026") == date(2026, 11, 30)
    assert parse_deadline("Dec 15, 2026 - Jan 20, 2027") == date(2027, 1, 20)
    assert parse_deadline("Dec 28 - 30, 2026") == date(2026, 12, 30)
    assert parse_deadline("Nov 30, 2026") == date(2026, 11, 30)
    assert parse_deadline("soon") is None
    assert parse_deadline("Feb 31, 2026") is None
    assert parse_deadline(None) is None


async def test_max_three_pages_and_dedupe():
    ctx, requests = _ctx(lambda req: httpx.Response(200, text=BODY))
    res = await run_source(DevpostSource(), ctx)
    assert len(res.items) == 6  # page 2 repeats page 1 -> no new items -> stop
    assert len(requests) == 2
    pages = []

    def distinct(req):
        n = int(req.url.params["page"])
        pages.append(n)
        d = json.loads(BODY)
        for h in d["hackathons"]:
            h["id"] += n * 100000
        return httpx.Response(200, json=d)

    ctx, _ = _ctx(distinct)
    res = await run_source(DevpostSource(), ctx)
    assert pages == [1, 2, 3] and len(res.items) == 18


async def test_empty_malformed_and_errors():
    for resp in (
        httpx.Response(200, json={"hackathons": []}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["x"]),
        httpx.Response(200, json={"hackathons": [{"title": "no url"}, 5]}),
        httpx.Response(500, text="boom"),
    ):
        ctx, _ = _ctx(lambda req, r=resp: r)
        res = await run_source(DevpostSource(), ctx)
        assert res.ok and res.items == []


def test_parse_page_skips_bad_items():
    assert parse_page(None) == []


def test_registered_and_enabled():
    assert "devpost" in REGISTRY
    src = REGISTRY["devpost"]()
    assert src.enabled(Settings.from_env(ENV), CFG)
    assert not src.enabled(Settings.from_env(ENV), AppConfig.from_dict({}))
