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
from src.sources.hn import HnSource, parse_header, parse_item, pick_story_id

FX = Path(__file__).parent / "fixtures"
SEARCH = (FX / "hn_search.json").read_text()
ITEM = (FX / "hn_item.json").read_text()
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict({"sources": {"hn": {"enabled": True}}})


def _ctx(search: httpx.Response, item: httpx.Response):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    requests = []

    def handler(req):
        requests.append(req)
        if req.url.path == "/robots.txt":
            return httpx.Response(404, text="Not Found")
        return search if req.url.path.endswith("search_by_date") else item

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http,
        settings=Settings.from_env(ENV),
        cfg=CFG,
        clock=clock,
        log=logging.getLogger("test"),
    )
    return ctx, requests


async def test_fixture_parses():
    ctx, requests = _ctx(httpx.Response(200, text=SEARCH), httpx.Response(200, text=ITEM))
    res = await run_source(HnSource(), ctx)
    assert res.ok
    items = res.items
    # 8 top-level comments in the fixture, one deleted
    assert len(items) == 7
    assert [r.url.path for r in requests if r.url.path != "/robots.txt"] == [
        "/api/v1/search_by_date",
        "/api/v1/items/50338728",
    ]
    first = items[0]
    assert first.source == "hn" and first.external_id == "50338732"
    assert first.company == "Simple Analytics"
    assert first.title == "DevOps Engineer"
    assert first.remote is True
    assert first.url == "https://news.ycombinator.com/item?id=50338732"
    assert first.posted_at is not None and first.posted_at.date().isoformat() == "2026-11-02"
    assert first.published_at == first.posted_at
    assert first.description.startswith("Simple Analytics | DevOps Engineer")
    by_id = {p.external_id: p for p in items}
    assert "50339999" not in by_id  # deleted comment skipped
    assert by_id["50339294"].kind == Kind.INTERNSHIP  # "Internships"
    assert by_id["50339641"].kind == Kind.INTERNSHIP and by_id["50339641"].company == "CNH"
    assert by_id["50338736"].kind == Kind.JOB
    assert by_id["50338736"].company == "Synteq"  # entities decoded via html_to_text
    assert by_id["50338736"].title == "Growth Lead & Systems Administrator"
    assert "50338733" not in by_id  # nested replies are not postings


def test_header_acme_intern_remote():
    comment = {
        "id": 1,
        "text": "Acme | ML Engineer Intern | Remote (EU)<p>We build things.",
        "created_at": "2026-11-02T16:00:00.000Z",
    }
    (p,) = parse_item({"children": [comment]})
    assert p.company == "Acme"
    assert p.title == "ML Engineer Intern"
    assert p.kind == Kind.INTERNSHIP and p.remote is True
    assert p.location == ""  # "Remote (EU)" is treated as remote, not a location
    assert p.description == "Acme | ML Engineer Intern | Remote (EU)\nWe build things."


def test_internal_is_not_intern():
    (p,) = parse_item(
        {"children": [{"id": 2, "text": "Acme | Internal Tools Engineer | Berlin<p>x"}]}
    )
    assert p.kind == Kind.JOB and p.location == "Berlin"


def test_header_without_role_falls_back():
    company, title, _ = parse_header("Shepherd (Series B) | ONSITE | San Francisco, CA")
    assert company == "Shepherd (Series B)"
    assert title.startswith("Shepherd (Series B) | ONSITE")
    assert len(parse_header("x" * 300)[1]) == 80


def test_picks_hiring_not_wants_to_be_hired():
    search = json.loads(SEARCH)
    search["hits"].sort(key=lambda h: "hired" not in h["title"])  # "wants to be hired" first
    assert "hired" in search["hits"][0]["title"]
    assert pick_story_id(search) == 50338728
    only_other = {"hits": [h for h in search["hits"] if "hired" in h["title"]]}
    assert pick_story_id(only_other) is None


async def test_empty_hits():
    ctx, _ = _ctx(httpx.Response(200, text='{"hits": []}'), httpx.Response(200, text=ITEM))
    res = await run_source(HnSource(), ctx)
    assert res.ok and res.items == []


async def test_malformed_json():
    ctx, _ = _ctx(httpx.Response(200, text="{not json"), httpx.Response(200, text=ITEM))
    assert (await run_source(HnSource(), ctx)).items == []
    ctx, _ = _ctx(httpx.Response(200, text=SEARCH), httpx.Response(200, text="<html>"))
    assert (await run_source(HnSource(), ctx)).items == []


def test_malformed_shapes():
    assert parse_item([1]) == []
    assert parse_item({"children": "x"}) == []
    assert parse_item({"children": [None, "junk", {"id": 1}, {"id": 2, "text": ""}]}) == []
    assert pick_story_id([1]) is None
    assert pick_story_id({"hits": [None, {"title": "Ask HN: Who is hiring?"}]}) is None


async def test_http_500():
    ctx, _ = _ctx(httpx.Response(500, text="boom"), httpx.Response(500, text="boom"))
    res = await run_source(HnSource(), ctx)
    assert not res.ok and res.error and res.items == []
    ctx, _ = _ctx(httpx.Response(403, text="no"), httpx.Response(403, text="no"))
    res = await run_source(HnSource(), ctx)
    assert not res.ok and res.error and res.items == []
    ctx, _ = _ctx(httpx.Response(200, text=SEARCH), httpx.Response(500, text="boom"))
    res = await run_source(HnSource(), ctx)
    assert not res.ok and res.error and res.items == []


def test_registered_and_enabled():
    assert "hn" in REGISTRY
    s = Settings.from_env(ENV)
    assert HnSource().enabled(s, CFG)
    off = AppConfig.from_dict({"sources": {"hn": {"enabled": False}}})
    assert not HnSource().enabled(s, off)
