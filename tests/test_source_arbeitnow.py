import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.arbeitnow import ArbeitnowSource, parse_jobs
from src.sources.base import REGISTRY, SourceContext, run_source

FX = Path(__file__).parent / "fixtures"
DATA = json.loads((FX / "arbeitnow_api.json").read_text())
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict({"sources": {"arbeitnow": {"enabled": True}}})


def _ctx(api, cfg=CFG):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 8, 12, tzinfo=UTC))
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        seen.append(req)
        return api(req)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=cfg, clock=clock, log=logging.getLogger("t")
    )
    return ctx, seen


def test_fixture_parses():
    items = parse_jobs(DATA)
    assert len(items) == 4
    first = items[0]
    assert first.source == "arbeitnow"
    assert first.external_id == "remote-developer-advocate-germany-439959"
    assert first.title == "Developer Advocate (Germany)" and first.company == "Adapty.io"
    assert first.url.startswith("https://www.arbeitnow.com/jobs/companies/adaptyio/")
    assert first.location == "Remote" and first.remote is True
    assert first.kind is Kind.JOB
    assert first.published_at == datetime.fromtimestamp(1794109213, UTC) == first.posted_at
    assert first.description and "<" not in first.description
    assert items[1].remote is False and items[1].location == "Munich"
    assert "Full Time" in items[1].raw_tags and "Client Roles" in items[1].raw_tags


def test_intern_kind_from_title_or_job_type():
    job = DATA["data"][0]
    assert parse_jobs({"data": [{**job, "title": "Data Intern"}]})[0].kind is Kind.INTERNSHIP
    assert parse_jobs({"data": [{**job, "job_types": ["Internship"]}]})[0].kind is Kind.INTERNSHIP
    assert parse_jobs({"data": [{**job, "title": "Internal Tools"}]})[0].kind is Kind.JOB


def test_entity_encoded_description_is_decoded():
    job = {**DATA["data"][0], "description": "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"}
    assert parse_jobs({"data": [job]})[0].description == "Hello & welcome"


def test_non_arbeitnow_url_is_skipped():
    job = DATA["data"][0]
    bad = [
        {**job, "url": "https://evil.example/jobs/x"},
        {**job, "url": "https://www.arbeitnow.com.evil.example/jobs/x"},
        {**job, "url": "https://arbeitnow.com/jobs/x"},
        {**job, "url": ""},
    ]
    assert parse_jobs({"data": bad}) == []


async def test_fetch_one_get_page_one_only():
    ctx, seen = _ctx(lambda r: httpx.Response(200, json=DATA))
    res = await run_source(ArbeitnowSource(), ctx)
    assert res.ok and len(res.items) == 4
    assert len(seen) == 1 and seen[0].url.path == "/api/job-board-api"
    assert "page" not in seen[0].url.params


async def test_empty_malformed_and_http_error():
    for resp in (httpx.Response(200, json={"data": []}), httpx.Response(200, json=["x"])):
        ctx, _ = _ctx(lambda r, x=resp: x)
        res = await run_source(ArbeitnowSource(), ctx)
        assert res.items == []
    for resp in (
        httpx.Response(200, text="{not json"),
        httpx.Response(500, text="boom"),
        httpx.Response(403, text="no"),
    ):
        ctx, _ = _ctx(lambda r, x=resp: x)
        res = await run_source(ArbeitnowSource(), ctx)
        assert not res.ok and res.error and res.items == []


async def test_second_fetch_within_hour_is_cached():
    ctx, seen = _ctx(lambda r: httpx.Response(200, json=DATA))
    await run_source(ArbeitnowSource(), ctx)
    res = await run_source(ArbeitnowSource(), ctx)
    assert len(seen) == 1 and len(res.items) == 4


async def test_max_pages_two_fetches_page_two():
    cfg = AppConfig.from_dict({"sources": {"arbeitnow": {"enabled": True, "max_pages": 2}}})
    ctx, seen = _ctx(lambda r: httpx.Response(200, json=DATA), cfg)
    res = await run_source(ArbeitnowSource(), ctx)
    assert [r.url.params.get("page") for r in seen] == [None, "2"]
    assert len(res.items) == 4  # same slugs dedupe


@pytest.mark.parametrize("bad", [0, 3, "1", True, 1.5])
def test_max_pages_validated(bad):
    with pytest.raises(ValueError, match="max_pages"):
        AppConfig.from_dict({"sources": {"arbeitnow": {"max_pages": bad}}})


def test_registered_and_enabled():
    src = REGISTRY["arbeitnow"]()
    assert src.default_tier == "tier2" and src.min_poll_minutes == 60
    assert src.enabled(Settings.from_env(ENV), CFG)
    off = AppConfig.from_dict({"sources": {"arbeitnow": {"enabled": False}}})
    assert not src.enabled(Settings.from_env(ENV), off)
    assert not src.enabled(Settings.from_env(ENV), AppConfig.from_dict({}))
