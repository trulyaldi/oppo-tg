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
from src.sources.lever import LeverSource, parse_postings

FIXTURE = (Path(__file__).parent / "fixtures" / "lever_postings.json").read_text()


def _cfg(companies, names=None):
    return AppConfig.from_dict(
        {"sources": {"lever": {"enabled": True, "companies": companies, "names": names or {}}}}
    )


def _ctx(routes, cfg):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
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
        {"palantir": httpx.Response(200, text=FIXTURE)},
        _cfg(["palantir"], {"palantir": "Palantir"}),
    )
    res = await run_source(LeverSource(), ctx)
    assert res.ok
    items = res.items
    assert len(items) == 5
    first = items[0]
    assert first.source == "lever"
    assert first.company == "Palantir"
    assert first.title == "Deployment Strategist, Internship"
    assert first.url.startswith("https://jobs.lever.co/palantir/")
    assert first.external_id.startswith("palantir:")
    assert first.posted_at == datetime.fromtimestamp(1778517108152 / 1000, tz=UTC)
    assert first.posted_at.tzinfo is not None
    assert first.kind == Kind.INTERNSHIP
    assert items[2].kind == Kind.JOB
    assert items[4].remote is True
    assert items[0].remote is False
    assert any(r.url.params.get("mode") == "json" for r in requests if r.url.path != "/robots.txt")


def test_commitment_intern_maps_to_internship():
    data = [
        {
            "id": "a",
            "text": "Analyst",
            "hostedUrl": "https://jobs.lever.co/x/a",
            "createdAt": 1700000000000,
            "categories": {"commitment": "Intern", "location": "Remote - EU"},
        }
    ]
    (p,) = parse_postings(data, "x", "")
    assert p.kind == Kind.INTERNSHIP
    assert p.remote is True
    assert p.company == "x"


async def test_empty_list():
    ctx, _ = _ctx({"palantir": httpx.Response(200, text="[]")}, _cfg(["palantir"]))
    res = await run_source(LeverSource(), ctx)
    assert res.ok and res.items == []


async def test_malformed_json():
    ctx, _ = _ctx({"palantir": httpx.Response(200, text="{not json")}, _cfg(["palantir"]))
    res = await run_source(LeverSource(), ctx)
    assert res.items == []


async def test_malformed_shape_and_entries():
    assert parse_postings({"a": 1}, "x", "") == []
    assert parse_postings([{"id": "1"}, "junk", None], "x", "") == []


async def test_http_500_and_isolation():
    ctx, _ = _ctx(
        {
            "bad": httpx.Response(500, text="boom"),
            "palantir": httpx.Response(200, text=FIXTURE),
        },
        _cfg(["bad", "palantir"]),
    )
    res = await run_source(LeverSource(), ctx)
    assert len(res.items) == 5


async def test_http_500_only():
    ctx, _ = _ctx({"bad": httpx.Response(500, text="boom")}, _cfg(["bad"]))
    res = await run_source(LeverSource(), ctx)
    assert res.items == []


def test_registered_and_enabled():
    assert "lever" in REGISTRY
    src = LeverSource()
    s = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    assert src.enabled(s, _cfg(["a"]))
    assert not src.enabled(s, _cfg([]))
