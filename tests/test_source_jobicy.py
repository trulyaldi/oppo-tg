import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.base import REGISTRY, SourceContext, run_source
from src.sources.jobicy import JobicySource, parse_jobs

FX = Path(__file__).parent / "fixtures"
DATA = json.loads((FX / "jobicy_jobs.json").read_text())
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict(
    {"sources": {"jobicy": {"enabled": True, "industries": ["data-science", "engineering"]}}}
)


def _ctx(api, cfg=CFG):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 6, 12, tzinfo=UTC))
    api_requests = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")
        api_requests.append(req)
        return api(req)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=cfg, clock=clock, log=logging.getLogger("t")
    )
    return ctx, api_requests


def test_fixture_parses():
    items = parse_jobs(DATA)
    assert len(items) == 5
    assert all("jobicy.com/" in p.url and p.remote is True for p in items)
    assert all("<" not in p.description and p.description for p in items)
    first = items[0]
    assert first.source == "jobicy" and first.external_id == "154739"
    assert first.company == "Doximity" and first.location == "USA"
    assert first.kind is Kind.JOB
    assert first.posted_at == datetime(2026, 11, 6, 3, 20, 12, tzinfo=UTC)
    assert "Data Science & Analytics" in first.raw_tags and "Full-Time" in first.raw_tags


def test_intern_title_gives_internship():
    intern = parse_jobs(DATA)[-1]
    assert intern.kind is Kind.INTERNSHIP
    assert intern.location == "Anywhere"
    # "Internal" must not match
    data = {"jobs": [{**DATA["jobs"][0], "jobTitle": "Internal Tools Engineer"}]}
    assert parse_jobs(data)[0].kind is Kind.JOB


async def test_fetch_dedupes_across_industries_and_params():
    ctx, reqs = _ctx(lambda r: httpx.Response(200, json=DATA))
    res = await run_source(JobicySource(), ctx)
    assert res.ok and len(res.items) == 5
    assert [r.url.params["industry"] for r in reqs] == ["data-science", "engineering"]
    assert all(r.url.path == "/api/jobs" and r.url.params["count"] == "100" for r in reqs)


async def test_empty_malformed_and_http_error():
    for resp in (
        httpx.Response(200, json={"jobs": []}),
        httpx.Response(200, json=["x"]),
    ):
        ctx, _ = _ctx(lambda r, x=resp: x)
        res = await run_source(JobicySource(), ctx)
        assert res.ok and res.items == []
    for resp in (
        httpx.Response(200, text="{not json"),
        httpx.Response(500, text="boom"),
        httpx.Response(403, text="no"),
    ):
        ctx, _ = _ctx(lambda r, x=resp: x)
        res = await run_source(JobicySource(), ctx)
        assert not res.ok and res.error and res.items == []


async def test_one_failing_query_keeps_the_other():
    def api(req):
        if req.url.params["industry"] == "data-science":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=DATA)

    ctx, _ = _ctx(api)
    res = await run_source(JobicySource(), ctx)
    assert res.ok and len(res.items) == 5


async def test_second_fetch_within_hour_is_cached():
    ctx, reqs = _ctx(lambda r: httpx.Response(200, json=DATA))
    await run_source(JobicySource(), ctx)
    n = len(reqs)
    res = await run_source(JobicySource(), ctx)
    assert len(reqs) == n and len(res.items) == 5


async def test_robots_disallow_skips_fetch():
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 6, tzinfo=UTC))
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
        seen.append(req)
        return httpx.Response(200, json=DATA)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=CFG, clock=clock, log=logging.getLogger("t")
    )
    res = await run_source(JobicySource(), ctx)
    assert res.items == [] and seen == []


def test_registered_and_enabled():
    assert "jobicy" in REGISTRY
    src = REGISTRY["jobicy"]()
    assert src.enabled(Settings.from_env(ENV), CFG)
    off = AppConfig.from_dict({"sources": {"jobicy": {"enabled": False, "industries": ["x"]}}})
    assert not src.enabled(Settings.from_env(ENV), off)
    assert not src.enabled(Settings.from_env(ENV), AppConfig.from_dict({}))
