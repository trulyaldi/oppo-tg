import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.base import SourceContext, run_source
from src.sources.greenhouse import GreenhouseSource, parse_jobs

FIXTURE = (Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text()


def _cfg(boards, names=None):
    return AppConfig.from_dict(
        {"sources": {"greenhouse": {"enabled": True, "boards": boards, "names": names or {}}}}
    )


def _ctx(routes, cfg):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")
        r = routes[req.url.path.split("/")[3]]
        return r(req) if callable(r) else r

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http,
        settings=Settings.from_env(
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
        ),
        cfg=cfg,
        clock=clock,
        log=logging.getLogger("test"),
    )
    return ctx, requests


async def test_fixture_parses():
    ctx, requests = _ctx(
        {"jumptrading": httpx.Response(200, text=FIXTURE)},
        _cfg(["jumptrading"], {"jumptrading": "Jump Trading"}),
    )
    res = await run_source(GreenhouseSource(), ctx)
    assert res.ok
    items = res.items
    assert len(items) >= 3
    for p in items:
        assert p.title
        assert p.url.startswith("https://")
        assert "<" not in p.description and "&lt;" not in p.description
        assert p.description
        assert p.company == "Jump Trading"
        assert p.external_id.startswith("jumptrading:")
        assert p.posted_at is not None and p.posted_at.tzinfo is not None
    interns = [p for p in items if "intern" in p.title.lower()]
    assert interns and all(p.kind == Kind.INTERNSHIP for p in interns)
    assert any(p.kind == Kind.JOB for p in items)
    assert {r.url.params["content"] for r in requests if r.url.path != "/robots.txt"} == {"true"}


async def test_robots_checked_for_api_host():
    ctx, requests = _ctx({"x": httpx.Response(200, text=FIXTURE)}, _cfg(["x"]))
    await run_source(GreenhouseSource(), ctx)
    assert any(r.url.path == "/robots.txt" for r in requests)


async def test_company_falls_back_to_payload():
    ctx, _ = _ctx({"x": httpx.Response(200, text=FIXTURE)}, _cfg(["x"]))
    res = await run_source(GreenhouseSource(), ctx)
    assert res.items[0].company


async def test_empty_response():
    ctx, _ = _ctx({"x": httpx.Response(200, json={"jobs": []})}, _cfg(["x"]))
    assert (await run_source(GreenhouseSource(), ctx)).items == []


async def test_malformed_json():
    ctx, _ = _ctx({"x": httpx.Response(200, text="<html>not json")}, _cfg(["x"]))
    res = await run_source(GreenhouseSource(), ctx)
    assert not res.ok and res.error and res.items == []


async def test_http_error():
    ctx, _ = _ctx({"x": httpx.Response(500)}, _cfg(["x"]))
    res = await run_source(GreenhouseSource(), ctx)
    assert not res.ok and res.error and res.items == []
    ctx, _ = _ctx({"x": httpx.Response(403)}, _cfg(["x"]))
    res = await run_source(GreenhouseSource(), ctx)
    assert not res.ok and res.error and res.items == []


async def test_one_bad_board_does_not_block_other():
    routes = {"bad": httpx.Response(500), "good": httpx.Response(200, text=FIXTURE)}
    ctx, _ = _ctx(routes, _cfg(["bad", "good"]))
    res = await run_source(GreenhouseSource(), ctx)
    assert res.ok
    assert len(res.items) >= 3
    assert all(p.external_id.startswith("good:") for p in res.items)


def test_parse_edge_cases():
    data = {
        "jobs": [
            {
                "id": 1,
                "title": "Summer Analyst",
                "absolute_url": "https://x.io/1",
                "location": {"name": "Remote - US"},
                "content": "&lt;p&gt;Hi&lt;/p&gt;",
                "updated_at": "2026-05-01T10:00:00-04:00",
                "application_deadline": "2026-06-01T00:00:00Z",
            },
            {"id": 2, "title": "bad url", "absolute_url": "javascript:x"},
            {"title": "no id", "absolute_url": "https://x.io/3"},
            "junk",
        ]
    }
    items = parse_jobs(data, "t", "T")
    assert len(items) == 1
    p = items[0]
    assert p.kind == Kind.INTERNSHIP and p.remote is True and p.description == "Hi"
    assert p.posted_at == datetime(2026, 5, 1, 14, 0, tzinfo=UTC)
    assert p.deadline.isoformat() == "2026-06-01"
    assert parse_jobs([], "t", "T") == [] and parse_jobs({"jobs": "x"}, "t", "T") == []


def test_enabled():
    s = GreenhouseSource()
    assert s.enabled(None, _cfg(["a"]))
    assert not s.enabled(None, _cfg([]))
    assert not s.enabled(None, AppConfig())


def test_internal_is_not_internship():
    titles = {
        "Internal Applications Engineer": Kind.JOB,
        "International Tax Analyst": Kind.JOB,
        "Software Engineer Intern": Kind.INTERNSHIP,
        "Internship 2027": Kind.INTERNSHIP,
        "Summer Analyst": Kind.INTERNSHIP,
    }
    data = {
        "jobs": [
            {"id": i, "title": t, "absolute_url": f"https://example.com/{i}"}
            for i, t in enumerate(titles)
        ]
    }
    got = {p.title: p.kind for p in parse_jobs(data, "x", "")}
    assert got == titles
