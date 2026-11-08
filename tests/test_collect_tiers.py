"""FC-10: tiered collection and the pipeline lock (D-186, D-207). Fixtures and fakes only."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.pipeline.run import collect_all, collect_tier, pipeline_lock, run_tier_cycle

FIX = Path(__file__).parent / "fixtures"
GH = (FIX / "greenhouse_jobs.json").read_text()
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
)
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>
<item><title>{title}</title><link>https://rss.example/1</link><guid>g1</guid>
<description>d</description></item></channel></rss>"""
JOBICY = json.dumps({"jobs": []})


def make(sources, handler=None, filters=None):
    cfg = AppConfig.from_dict(
        {
            "sources": sources,
            "filters": filters or {},
        }
    )
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(T0)
    seen: list[str] = []

    def default(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        seen.append(str(req.url))
        if "greenhouse" in req.url.host:
            return httpx.Response(200, text=GH)
        if "jobicy" in req.url.host:
            return httpx.Response(200, text=JOBICY)
        return httpx.Response(200, text="<html>x</html>")

    async def nosleep(_):
        pass

    http = build_client(
        conn, clock, transport=httpx.MockTransport(handler or default), sleep=nosleep
    )
    return conn, http, cfg, clock, seen


async def test_tier1_only_config_boards_and_tier3_only_pagewatch():
    conn, http, cfg, clock, seen = make(
        {
            "greenhouse": {"enabled": True, "boards": ["a", "b"]},
            "pagewatch": {"enabled": True, "pages": [{"name": "p", "url": "https://pw.example/x"}]},
            "jobicy": {"enabled": True},
        }
    )
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert len(seen) == 2 and all("greenhouse" in u for u in seen)
    states = {
        (r["source"], r["target"]): r["tier"] for r in conn.execute("SELECT * FROM target_state")
    }
    assert set(states) == {("greenhouse", "a"), ("greenhouse", "b")}
    assert set(states.values()) == {"tier1"}
    seen.clear()
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier3")
    assert len(seen) == 1 and "pw.example" in seen[0]
    seen.clear()
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier2")
    assert seen and all("jobicy" in u for u in seen)


async def test_source_tier_override():
    conn, http, cfg, clock, seen = make(
        {"greenhouse": {"enabled": True, "boards": ["a"], "tier": "tier2"}}
    )
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert seen == []
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier2")
    assert len(seen) == 1


async def test_same_token_listed_twice_fetched_once():
    conn, http, cfg, clock, seen = make({"greenhouse": {"enabled": True, "boards": ["a", "a"]}})
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert len(seen) == 1


async def test_jobicy_min_poll_floor():
    conn, http, cfg, clock, seen = make({"jobicy": {"enabled": True}})
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier2")
    n = len(seen)
    assert n >= 1
    clock.set(T0 + timedelta(minutes=30))
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier2")
    assert len(seen) == n
    clock.set(T0 + timedelta(minutes=61))
    conn.execute("DELETE FROM http_cache")
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier2")
    assert len(seen) > n


async def test_collect_all_applies_floors():
    conn, http, cfg, clock, seen = make({"jobicy": {"enabled": True}})
    await collect_all(conn, http, SETTINGS, cfg, clock)
    n = len(seen)
    clock.set(T0 + timedelta(minutes=30))
    await collect_all(conn, http, SETTINGS, cfg, clock)
    assert len(seen) == n


async def test_no_validator_fallback_for_tier1():
    conn, http, cfg, clock, seen = make({"greenhouse": {"enabled": True, "boards": ["a"]}})
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert len(seen) == 1
    assert dbm.get_target_state(conn, "greenhouse", "a")["has_validators"] == 0
    conn.execute("DELETE FROM http_cache")
    clock.set(T0 + timedelta(minutes=30))
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert len(seen) == 1  # within no_validator_min_minutes
    clock.set(T0 + timedelta(minutes=61))
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert len(seen) == 2


async def test_tier1_revalidates_and_gets_304():
    calls = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        calls.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=GH, headers={"ETag": '"v1"'})

    conn, http, cfg, clock, _ = make(
        {"greenhouse": {"enabled": True, "boards": ["a"]}}, handler=handler
    )
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    clock.set(T0 + timedelta(minutes=15))
    await collect_tier(conn, http, SETTINGS, cfg, clock, "tier1")
    assert calls == [None, '"v1"']  # TTL bypassed, conditional request sent


async def test_dedupe_across_tiers_either_order():
    gh = json.loads(GH)["jobs"][0]
    title, company = gh["title"], gh["company_name"]

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if "greenhouse" in req.url.host:
            return httpx.Response(200, text=json.dumps({"jobs": [gh]}))
        return httpx.Response(200, text=RSS.format(title=title))

    for order in (("tier1", "tier2"), ("tier2", "tier1")):
        conn, http, cfg, clock, _ = make(
            {
                "greenhouse": {"enabled": True, "boards": ["a"]},
                "rss": {
                    "enabled": True,
                    "feeds": [{"name": company, "url": "https://rss.example/feed"}],
                },
            },
            handler=handler,
        )
        for tier in order:
            await collect_tier(conn, http, SETTINGS, cfg, clock, tier)
        rows = conn.execute("SELECT source, status FROM postings").fetchall()
        assert {r["source"] for r in rows} == {"greenhouse", "rss"}
        assert [r["status"] for r in rows].count("duplicate") == 1
        assert sum(r["status"] != "duplicate" for r in rows) == 1


async def test_collect_runs_outside_lock_and_steps_serialise():
    log: list[str] = []
    gate = asyncio.Event()

    async def slow_collect():
        log.append("t2 collect start")
        await gate.wait()

    async def fast_collect():
        log.append("t1 collect")

    def steps(tag):
        async def process():
            log.append(f"{tag} process")

        async def score():
            log.append(f"{tag} score start")
            await asyncio.sleep(0.01)
            log.append(f"{tag} score end")

        async def alerts():
            log.append(f"{tag} alerts")

        return process, score, alerts

    p2, s2, a2 = steps("t2")
    p1, s1, a1 = steps("t1")
    t2 = asyncio.ensure_future(
        run_tier_cycle("tier2", collect=slow_collect, process=p2, score=s2, alerts=a2)
    )
    await asyncio.sleep(0)
    # tier1 completes its whole cycle while tier2's collect is still blocked
    await run_tier_cycle("tier1", collect=fast_collect, process=p1, score=s1, alerts=a1)
    assert "t2 collect start" in log and "t2 score start" not in log
    assert log[-1] == "t1 alerts"
    gate.set()
    await t2
    assert log.index("t2 score start") > log.index("t1 alerts")


async def test_two_tier_jobs_score_one_after_another():
    log: list[str] = []

    def steps(tag):
        async def noop():
            pass

        async def score():
            log.append(f"{tag} start")
            await asyncio.sleep(0.01)
            log.append(f"{tag} end")

        return noop, score

    n1, s1 = steps("a")
    n2, s2 = steps("b")
    await asyncio.gather(
        run_tier_cycle("tier1", collect=n1, process=n1, score=s1, alerts=n1),
        run_tier_cycle("tier2", collect=n2, process=n2, score=s2, alerts=n2),
    )
    assert log in (
        ["a start", "a end", "b start", "b end"],
        ["b start", "b end", "a start", "a end"],
    )


async def test_pipeline_lock_is_shared():
    assert pipeline_lock() is pipeline_lock()


async def test_kf17_hh_is_tier1_with_a_10_minute_floor():
    settings = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
            "ANTHROPIC_API_KEY": "k",
            "HH_ACCESS_TOKEN": "tok",
        }
    )
    hh_calls: list[str] = []
    gh_calls: list[str] = []

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if "hh.ru" in req.url.host:
            hh_calls.append(str(req.url))
            return httpx.Response(200, json={"items": [], "found": 0})
        gh_calls.append(str(req.url))
        return httpx.Response(200, text=GH)

    conn, http, cfg, clock, _ = make(
        {
            "hh": {"enabled": True, "queries": ["q1"]},
            "greenhouse": {"enabled": True, "boards": ["a"]},
        },
        handler=handler,
    )
    await collect_tier(conn, http, settings, cfg, clock, "tier2")
    assert not hh_calls  # not a tier2 source any more
    await collect_tier(conn, http, settings, cfg, clock, "tier1")
    assert len(hh_calls) == 1 and len(gh_calls) == 1  # hh and the config board
    clock.set(T0 + timedelta(minutes=5))
    await collect_tier(conn, http, settings, cfg, clock, "tier1")
    assert len(hh_calls) == 1  # inside the 10 minute floor
    clock.set(T0 + timedelta(minutes=11))
    await collect_tier(conn, http, settings, cfg, clock, "tier1")
    assert len(hh_calls) == 2  # not held back by the 60 minute no-validator wait
