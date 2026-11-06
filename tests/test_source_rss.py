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
from src.sources.rss import RssSource, parse_feed

FX = Path(__file__).parent / "fixtures"
RSS = (FX / "rss_sample.xml").read_text()
NSF = (FX / "rss_nsf.xml").read_text()
ATOM = (FX / "atom_sample.xml").read_text()


def _cfg(feeds):
    return AppConfig.from_dict({"sources": {"rss": {"enabled": True, "feeds": feeds}}})


def _ctx(routes, cfg):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return routes[req.url.host]

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    return SourceContext(
        http=http,
        settings=Settings.from_env(
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
        ),
        cfg=cfg,
        clock=clock,
        log=logging.getLogger("test"),
    )


def _feed(host, kind="job", name="F"):
    return {"url": f"https://{host}/feed", "name": name, "kind": kind}


def test_rss_fixture_parses():
    items = parse_feed(RSS, "HN Jobs", "job")
    assert len(items) == 4
    first = items[0]
    assert first.source == "rss"
    assert first.title == "Bild AI (YC W25) Is Hiring Founding Product Engineers"
    assert first.url == "https://www.ycombinator.com/companies/bild-ai/jobs"
    assert first.posted_at == datetime(2026, 11, 5, 17, 1, 23, tzinfo=UTC)
    assert first.company == "HN Jobs"
    assert first.kind == Kind.JOB
    assert first.external_id.startswith("HN Jobs:")


def test_rss_nsf_kind_and_deadline():
    items = parse_feed(NSF, "NSF", "research")
    assert len(items) == 4
    assert all(i.kind == Kind.RESEARCH for i in items)
    assert items[0].deadline == date(2026, 11, 10)
    assert "Research Initiation" in items[0].title


def test_atom_fixture_parses_and_skips_no_link():
    items = parse_feed(ATOM, "Ex", "fellowship")
    assert [i.title for i in items] == [
        "Summer Research Internship in Machine Learning",
        "Visiting Fellow, Computational Biology",
    ]
    assert items[0].url == "https://example.org/fellowships/ml-internship"
    assert items[0].posted_at == datetime(2026, 11, 3, 9, 30, tzinfo=UTC)
    assert items[1].posted_at == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    assert items[0].company == "Example Lab"
    assert "<" not in items[0].description
    assert all(i.kind == Kind.FELLOWSHIP for i in items)


def test_intern_word_boundary_for_job_kind():
    xml = (
        '<rss version="2.0"><channel><title>t</title>'
        "<item><title>Data Intern</title><link>https://a.example/1</link></item>"
        "<item><title>Internal Tools Engineer</title><link>https://a.example/2</link></item>"
        "</channel></rss>"
    )
    a, b = parse_feed(xml, "x", "job")
    assert a.kind == Kind.INTERNSHIP
    assert b.kind == Kind.JOB


def test_unknown_kind_falls_back_to_other():
    (p,) = parse_feed(ATOM, "Ex", "bogus")[:1]
    assert p.kind == Kind.OTHER


async def test_fetch_end_to_end_and_isolation():
    ctx = _ctx(
        {
            "a.example": httpx.Response(200, text=RSS),
            "b.example": httpx.Response(500, text="boom"),
            "c.example": httpx.Response(200, text=ATOM),
        },
        _cfg([_feed("a.example"), _feed("b.example"), _feed("c.example", "fellowship")]),
    )
    res = await run_source(RssSource(), ctx)
    assert res.ok
    assert len(res.items) == 4 + 2


async def test_garbage_xml_returns_empty():
    ctx = _ctx(
        {"a.example": httpx.Response(200, text="<<not xml at all")}, _cfg([_feed("a.example")])
    )
    res = await run_source(RssSource(), ctx)
    assert res.ok and res.items == []
    assert parse_feed("", "x", "job") == []


async def test_http_500_returns_empty():
    ctx = _ctx({"a.example": httpx.Response(500, text="boom")}, _cfg([_feed("a.example")]))
    res = await run_source(RssSource(), ctx)
    assert not res.ok and res.error and res.items == []
    ctx = _ctx({"a.example": httpx.Response(403, text="no")}, _cfg([_feed("a.example")]))
    res = await run_source(RssSource(), ctx)
    assert not res.ok and res.error and res.items == []


def test_registered_and_enabled():
    assert "rss" in REGISTRY
    s = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    assert RssSource().enabled(s, _cfg([_feed("a.example")]))
    assert not RssSource().enabled(s, _cfg([]))
