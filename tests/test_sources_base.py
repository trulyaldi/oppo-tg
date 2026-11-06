import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import PoliteClient, build_client
from src.sources import base
from src.sources.base import REGISTRY, SourceContext, run_source

ROBOTS = (Path(__file__).parent / "fixtures" / "robots_disallow_jobs.txt").read_text()


def _env():
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    return conn, FixedClock(datetime(2026, 1, 1, tzinfo=UTC))


def _settings():
    return Settings.from_env(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
    )


class Boom:
    name = "boom"
    description = "x"

    def enabled(self, settings, cfg):
        return True

    async def fetch(self, ctx):
        raise RuntimeError("kaboom")


def _ctx(http):
    return SourceContext(
        http=http,
        settings=_settings(),
        cfg=AppConfig(),
        clock=FixedClock(datetime(2026, 1, 1, tzinfo=UTC)),
        log=logging.getLogger("test"),
    )


async def test_run_source_isolates_exception():
    conn, clock = _env()
    http = build_client(conn, clock, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    res = await run_source(Boom(), _ctx(http))
    assert res.ok is False
    assert res.items == []
    assert "kaboom" in res.error
    await http.aclose()


def test_greenhouse_registered():
    assert "greenhouse" in REGISTRY
    src = REGISTRY["greenhouse"]()
    assert src.name == "greenhouse"


def test_register_duplicate_rejected():
    with pytest.raises(ValueError):
        base.register("greenhouse")(lambda: None)


def test_context_rejects_plain_client():
    with pytest.raises(ValueError, match="db and clock"):
        _ctx(PoliteClient())


def test_build_client_requires_db_and_clock():
    conn, clock = _env()
    with pytest.raises(ValueError):
        build_client(None, clock)
    with pytest.raises(ValueError):
        build_client(conn, None)


async def test_build_client_enforces_robots():
    conn, clock = _env()

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text="{}")

    http = build_client(conn, clock, transport=httpx.MockTransport(handler))
    # robots fixture disallows /jobs
    assert await http.get_text("https://example.com/jobs/1") is None
    await http.aclose()
