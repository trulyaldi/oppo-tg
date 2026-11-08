from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import PoliteClient
from src.pipeline.run import collect_all
from src.sources.base import SourceContext

FIXTURE = (Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text()
URL = "https://api.test/data"
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
)


def _client(respond):
    reqs: list[httpx.Request] = []
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        reqs.append(req)
        return respond(req)

    async def nosleep(_):
        pass

    c = PoliteClient(
        db=conn,
        clock=clock,
        transport=httpx.MockTransport(handler),
        min_interval_s=0,
        sleep=nosleep,
    )
    return c, conn, clock, reqs


def _etag(req):
    if req.headers.get("If-None-Match") == "v1":
        return httpx.Response(304)
    return httpx.Response(200, text="body", headers={"ETag": "v1"})


@pytest.mark.asyncio
async def test_revalidate_sends_validator_despite_fresh_cache():
    c, _, _, reqs = _client(_etag)
    first = await c.fetch(URL, ttl_s=3600)
    assert first.text == "body" and first.has_validators and not first.not_modified
    hit = await c.fetch(URL, ttl_s=3600)
    assert hit.from_cache and len(reqs) == 1
    res = await c.fetch(URL, ttl_s=3600, revalidate=True)
    assert len(reqs) == 2
    assert reqs[1].headers["If-None-Match"] == "v1"
    assert res.not_modified and res.text == "body" and res.status == 304
    assert res.has_validators and not res.from_cache


@pytest.mark.asyncio
async def test_no_validators_on_plain_200():
    c, _, _, _ = _client(lambda r: httpx.Response(200, text="x"))
    res = await c.fetch(URL, ttl_s=60, revalidate=True)
    assert res.status == 200 and res.has_validators is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403])
async def test_4xx_returns_status_without_text(status):
    c, _, _, _ = _client(lambda r: httpx.Response(status, text="no"))
    res = await c.fetch(URL, ttl_s=60)
    assert res.status == status and res.text is None and res.error
    assert await c.get_text(URL) is None


@pytest.mark.asyncio
async def test_304_without_cache_is_error():
    c, _, _, _ = _client(lambda r: httpx.Response(304))
    res = await c.fetch(URL, ttl_s=60)
    assert res.text is None and res.error


@pytest.mark.asyncio
async def test_revalidate_without_ttl_still_caches_and_get_json_unchanged():
    c, _, _, reqs = _client(_etag)
    await c.fetch(URL, revalidate=True)
    res = await c.fetch(URL, revalidate=True)
    assert res.not_modified
    assert await c.get_json(URL) is None  # "body" is not JSON


@pytest.mark.asyncio
async def test_tier1_board_304_bumps_last_seen_and_inserts_nothing():
    cfg = AppConfig.from_dict(
        {
            "sources": {"greenhouse": {"enabled": True, "boards": ["a"]}},
            "filters": {},
        }
    )

    def respond(req):
        if req.headers.get("If-None-Match") == "e1":
            return httpx.Response(304)
        return httpx.Response(200, text=FIXTURE, headers={"ETag": "e1"})

    c, conn, clock, reqs = _client(respond)
    await collect_all(conn, c, SETTINGS, cfg, clock)
    n = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    assert n > 0
    st = dbm.get_target_state(conn, "greenhouse", "a")
    assert st["has_validators"] == 1
    ctx = SourceContext(http=c, settings=SETTINGS, cfg=cfg, clock=clock, revalidate=True)
    from src.sources.base import run_target
    from src.sources.greenhouse import GreenhouseSource

    src = GreenhouseSource()
    t = src.targets(ctx)[0]
    tr = await run_target(src, ctx, t)
    assert reqs[-1].headers["If-None-Match"] == "e1"
    assert tr.ok and tr.has_validators is True and len(tr.items) == n
    before = conn.execute("SELECT MAX(last_seen_at) FROM postings").fetchone()[0]
    later = datetime(2026, 1, 2, tzinfo=UTC)
    for p in tr.items:
        from src.pipeline.dedupe import fingerprint

        out = dbm.upsert_posting(conn, p, fingerprint(p), later, target="a", baseline=False)
        assert out is not dbm.Upsert.INSERTED
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == n
    assert conn.execute("SELECT MIN(last_seen_at) FROM postings").fetchone()[0] > before
