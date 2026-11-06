"""Remote OK source tests.

tests/fixtures/remoteok_api.json is HAND-WRITTEN from the documented response shape (D-178),
NOT captured from a live response (see remoteok_api.NOTE.txt). A real-response check is
deferred to the final smoke test or to the user.
"""

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
from src.sources.remoteok import RemoteOKSource, parse_jobs

FX = Path(__file__).parent / "fixtures"
DATA = json.loads((FX / "remoteok_api.json").read_text())
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CFG = AppConfig.from_dict({"sources": {"remoteok": {"enabled": True}}})


def _ctx(api, cfg=CFG, robots="User-agent: *\nAllow: /\nCrawl-delay: 1\n"):
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 11, 6, 12, tzinfo=UTC))
    seen = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        seen.append(req)
        return api(req)

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    ctx = SourceContext(
        http=http, settings=Settings.from_env(ENV), cfg=cfg, clock=clock, log=logging.getLogger("t")
    )
    return ctx, seen


def test_legal_element_skipped_and_fixture_parses():
    items = parse_jobs(DATA)
    assert [p.external_id for p in items] == ["1001", "1002", "1003"]
    first = items[0]
    assert first.source == "remoteok" and first.company == "Acme" and first.remote is True
    assert first.url == "https://remoteok.com/remote-jobs/remote-1001"  # not apply_url
    assert "/l/" not in first.url
    assert first.description == "Build things & ship." or "<" not in first.description
    assert first.published_at == datetime(2026, 11, 6, 9, tzinfo=UTC)
    assert "python" in first.raw_tags


def test_date_fallback_and_internship():
    items = parse_jobs(DATA)
    assert items[2].published_at == datetime(2026, 11, 6, 9, tzinfo=UTC)  # epoch null -> date
    assert items[1].kind is Kind.INTERNSHIP and items[0].kind is Kind.JOB


def test_tags_filter_is_local_and_case_insensitive():
    assert [p.external_id for p in parse_jobs(DATA, ("Python",))] == ["1001"]
    assert parse_jobs(DATA, ("rust",)) == []


def test_malformed_shapes():
    assert parse_jobs({"jobs": []}) == [] and parse_jobs([]) == [] and parse_jobs(None) == []
    assert parse_jobs([DATA[0]]) == []


async def test_fetch_single_plain_get_and_cache():
    ctx, seen = _ctx(lambda r: httpx.Response(200, json=DATA))
    res = await run_source(RemoteOKSource(), ctx)
    assert res.ok and len(res.items) == 3
    assert len(seen) == 1 and seen[0].url.path == "/api" and not seen[0].url.params
    await run_source(RemoteOKSource(), ctx)
    assert len(seen) == 1  # cached within the hour


async def test_config_tags_filter_applied():
    cfg = AppConfig.from_dict({"sources": {"remoteok": {"enabled": True, "tags": ["python"]}}})
    ctx, _ = _ctx(lambda r: httpx.Response(200, json=DATA), cfg)
    res = await run_source(RemoteOKSource(), ctx)
    assert [p.external_id for p in res.items] == ["1001"]


async def test_empty_and_error_responses():
    ctx, _ = _ctx(lambda r: httpx.Response(200, json=[DATA[0]]))
    res = await run_source(RemoteOKSource(), ctx)
    assert res.ok and res.items == []
    for resp in (
        httpx.Response(200, text="{not json"),
        httpx.Response(500, text="boom"),
        httpx.Response(403, text="no"),
    ):
        ctx, _ = _ctx(lambda r, x=resp: x)
        res = await run_source(RemoteOKSource(), ctx)
        assert not res.ok and res.error and res.items == []


async def test_robots_disallow_skips_fetch():
    ctx, seen = _ctx(
        lambda r: httpx.Response(200, json=DATA), robots="User-agent: *\nDisallow: /\n"
    )
    res = await run_source(RemoteOKSource(), ctx)
    assert res.items == [] and seen == []


def test_registered_floor_and_enabled():
    assert "remoteok" in REGISTRY
    src = REGISTRY["remoteok"]()
    assert src.min_poll_minutes == 60 and src.default_tier == "tier2" and src.family == "remoteok"
    assert src.enabled(Settings.from_env(ENV), CFG)
    assert not src.enabled(Settings.from_env(ENV), AppConfig.from_dict({}))


def test_shipped_config():
    cfg = AppConfig.load(Path(__file__).parent.parent / "config.toml")
    assert cfg.source_enabled("remoteok") and cfg.source_tier("remoteok", "x") == "tier2"
    assert cfg.politeness.hosts["remoteok.com"] == 5.0
