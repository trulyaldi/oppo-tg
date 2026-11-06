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
from src.sources.pagewatch import PageWatchSource, extract_text

FX = Path(__file__).parent / "fixtures"
V1 = (FX / "page_v1.html").read_text()
V2 = (FX / "page_v2.html").read_text()
URL = "https://example.org/internships"


def _cfg(selector=None, enabled=True):
    page = {"url": URL, "name": "Example Fellowship", "kind": "fellowship"}
    if selector:
        page["css_selector"] = selector
    return AppConfig.from_dict({"sources": {"pagewatch": {"enabled": enabled, "pages": [page]}}})


class Harness:
    def __init__(self, cfg, body=V1, status=200):
        self.conn = dbm.connect(":memory:")
        dbm.migrate(self.conn)
        self.body = body
        self.status = status
        self.cfg = cfg
        self.clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    def run(self):
        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n")
            return httpx.Response(self.status, text=self.body)

        async def nosleep(_):
            pass

        http = build_client(
            self.conn, self.clock, transport=httpx.MockTransport(handler), sleep=nosleep
        )
        ctx = SourceContext(
            http=http,
            settings=Settings.from_env(
                {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
            ),
            cfg=self.cfg,
            clock=self.clock,
            log=logging.getLogger("test"),
        )
        return ctx


async def _fetch(h):
    # the response cache is shared via the DB; clear it so a new body is seen
    h.conn.execute("DELETE FROM http_cache")
    return await run_source(PageWatchSource(), h.run())


async def test_first_sight_then_unchanged_then_changed():
    h = Harness(_cfg())
    r = await _fetch(h)
    assert r.ok and len(r.items) == 1
    p = r.items[0]
    assert p.title == "Example Fellowship"
    assert p.kind == Kind.FELLOWSHIP
    assert p.external_id.startswith(URL + ":") and len(p.external_id) == len(URL) + 13
    assert "internship program" in p.description

    assert (await _fetch(h)).items == []

    h.body = V2
    r = await _fetch(h)
    assert len(r.items) == 1
    assert r.items[0].title == "Example Fellowship: page updated"
    assert "Apply by 15 December" in r.items[0].description
    assert (await _fetch(h)).items == []


async def test_script_and_whitespace_changes_ignored():
    h = Harness(_cfg())
    assert len((await _fetch(h)).items) == 1
    h.body = V1.replace("aaa111", "zzz999").replace("<p>", "\n  <p>  ")
    assert (await _fetch(h)).items == []


def test_selector_applied():
    assert extract_text(V1, ".deadlines") == "Applications for 2027 internships open in August."
    assert "Footer" not in extract_text(V1)
    assert "Home" not in extract_text(V1)  # <main> beats <body>
    assert "Footer" in extract_text("<body><p>x</p><footer>Footer</footer></body>")
    # unknown selector falls back to <main>
    assert extract_text(V1, ".nope").startswith("Internships")


async def test_selector_ignores_changes_outside():
    h = Harness(_cfg(".deadlines"))
    assert len((await _fetch(h)).items) == 1
    h.body = V1.replace("<h1>Internships</h1>", "<h1>Internships 2</h1>")
    assert (await _fetch(h)).items == []


async def test_http_error_returns_empty():
    h = Harness(_cfg(), status=500)
    r = await _fetch(h)
    assert not r.ok and r.error and r.items == []
    r = await _fetch(Harness(_cfg(), status=403))
    assert not r.ok and r.error and r.items == []


async def test_unchanged_page_is_ok_and_empty():
    h = Harness(_cfg())
    assert (await _fetch(h)).ok
    r = await _fetch(h)
    assert r.ok and r.items == []


async def test_malformed_and_empty_pages():
    for body in ("", "<script>x</script>", "<html></html>"):
        h = Harness(_cfg(), body=body)
        r = await _fetch(h)
        assert r.ok and r.items == []


async def test_robots_disallow_skips_page():
    h = Harness(_cfg())

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("page must not be fetched")

    async def nosleep(_):
        pass

    ctx = h.run()
    ctx.http = build_client(h.conn, h.clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    res = await run_source(PageWatchSource(), ctx)
    assert not res.ok and res.error and res.items == []


def test_registered_and_enabled():
    assert "pagewatch" in REGISTRY
    s = Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )
    assert PageWatchSource().enabled(s, _cfg())
    assert not PageWatchSource().enabled(s, _cfg(enabled=False))
    assert not PageWatchSource().enabled(s, AppConfig.from_dict({}))
