import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind
from src.sources.ashby import AshbySource, parse_board
from src.sources.base import REGISTRY, SourceContext, run_source

FIXTURE = (Path(__file__).parent / "fixtures" / "ashby_board.json").read_text()
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}


def _cfg(boards, names=None):
    return AppConfig.from_dict(
        {"sources": {"ashby": {"enabled": True, "boards": boards, "names": names or {}}}}
    )


def _ctx(routes, cfg):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(401, text="Unauthorized")
        return routes[req.url.path.rsplit("/", 1)[-1]]

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http,
        settings=Settings.from_env(ENV),
        cfg=cfg,
        clock=clock,
        log=logging.getLogger("test"),
    )
    return ctx, requests


async def test_fixture_parses():
    ctx, requests = _ctx(
        {"cohere": httpx.Response(200, text=FIXTURE)}, _cfg(["cohere"], {"cohere": "Cohere"})
    )
    res = await run_source(AshbySource(), ctx)
    assert res.ok
    items = res.items
    assert len(items) == 5
    first = items[0]
    assert first.source == "ashby"
    assert first.company == "Cohere"
    assert first.title == "Research Internship (Winter 2027)"
    assert first.url.startswith("https://jobs.ashbyhq.com/cohere/")
    assert first.external_id.startswith("cohere:")
    assert first.kind == Kind.INTERNSHIP
    assert first.remote is True
    assert first.posted_at == datetime(2026, 5, 1, 20, 26, 46, 572000, tzinfo=UTC)
    assert first.description
    assert items[2].kind == Kind.JOB
    assert items[3].remote is False
    api = [r for r in requests if r.url.path != "/robots.txt"]
    assert api and all(r.url.params.get("includeCompensation") == "false" for r in api)


def test_employment_type_intern_and_unlisted():
    data = {
        "jobs": [
            {
                "id": "a",
                "title": "Analyst",
                "jobUrl": "https://jobs.ashbyhq.com/x/a",
                "employmentType": "Intern",
                "isRemote": True,
                "publishedAt": "2026-01-01T00:00:00Z",
            },
            {"id": "b", "title": "Hidden", "jobUrl": "u", "isListed": False},
        ]
    }
    (p,) = parse_board(data, "x", "")
    assert p.kind == Kind.INTERNSHIP and p.remote is True and p.company == "x"


async def test_empty_jobs():
    ctx, _ = _ctx({"cohere": httpx.Response(200, text='{"jobs": []}')}, _cfg(["cohere"]))
    res = await run_source(AshbySource(), ctx)
    assert res.ok and res.items == []


async def test_malformed_json():
    ctx, _ = _ctx({"cohere": httpx.Response(200, text="{not json")}, _cfg(["cohere"]))
    res = await run_source(AshbySource(), ctx)
    assert res.items == []


def test_malformed_shape_and_entries():
    assert parse_board([1], "x", "") == []
    assert parse_board({"jobs": "x"}, "x", "") == []
    assert parse_board({"jobs": [{"id": "1"}, "junk", None]}, "x", "") == []


async def test_http_500_and_isolation():
    ctx, _ = _ctx(
        {"bad": httpx.Response(500, text="boom"), "cohere": httpx.Response(200, text=FIXTURE)},
        _cfg(["bad", "cohere"]),
    )
    res = await run_source(AshbySource(), ctx)
    assert len(res.items) == 5


async def test_http_500_only():
    ctx, _ = _ctx({"bad": httpx.Response(500, text="boom")}, _cfg(["bad"]))
    res = await run_source(AshbySource(), ctx)
    assert res.items == []


def test_registered_and_enabled():
    assert "ashby" in REGISTRY
    src = AshbySource()
    s = Settings.from_env(ENV)
    assert src.enabled(s, _cfg(["a"]))
    assert not src.enabled(s, _cfg([]))
