from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.pipeline.run import collect_all
from src.sources.base import Target, run_target, source_targets
from src.sources.greenhouse import GreenhouseSource
from src.sources.hn import HnSource

FIXTURE = (Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text()
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
)


def _setup(boards, statuses):
    cfg = AppConfig.from_dict(
        {
            "sources": {"greenhouse": {"enabled": True, "boards": boards}},
            "filters": {},
        }
    )
    conn = dbm.connect(":memory:")
    dbm.migrate(conn)
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))

    def handler(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        status = statuses[req.url.path.split("/")[3]]
        return httpx.Response(status, text=FIXTURE if status == 200 else "x")

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    return conn, http, cfg, clock


def _ctx(http, cfg, clock):
    from src.sources.base import SourceContext

    return SourceContext(http=http, settings=SETTINGS, cfg=cfg, clock=clock)


def test_single_target_source_default():
    conn, http, cfg, clock = _setup(["a"], {})
    assert source_targets(HnSource(), _ctx(http, cfg, clock)) == [Target("hn", "")]


def test_greenhouse_targets_equal_config_boards():
    conn, http, cfg, clock = _setup(["a", "b"], {})
    ts = source_targets(GreenhouseSource(), _ctx(http, cfg, clock))
    assert [t.key for t in ts] == ["a", "b"]
    assert all(t.origin == "config" and t.source == "greenhouse" for t in ts)


async def test_run_target_never_raises():
    conn, http, cfg, clock = _setup(["a"], {"a": 404})
    ctx = _ctx(http, cfg, clock)
    res = await run_target(GreenhouseSource(), ctx, Target("greenhouse", "a"))
    assert not res.ok and res.items == [] and res.error


async def test_collect_one_board_404_others_stored():
    conn, http, cfg, clock = _setup(["good", "bad"], {"good": 200, "bad": 404})
    stats = await collect_all(conn, http, SETTINGS, cfg, clock)
    assert stats["failed"] == 0 and stats["new"] > 0
    targets = {r["target"] for r in conn.execute("SELECT target FROM postings")}
    assert targets == {"good"}
    good = dbm.get_target_state(conn, "greenhouse", "good")
    bad = dbm.get_target_state(conn, "greenhouse", "bad")
    assert good["last_ok_at"] and good["last_error"] is None
    assert bad["last_attempt_at"] and bad["last_ok_at"] is None and bad["last_error"]
    run = conn.execute("SELECT ok, error FROM source_runs WHERE source='greenhouse'").fetchone()
    assert run["ok"] == 1 and run["error"] is None


async def test_collect_all_targets_failed_marks_source_failed():
    conn, http, cfg, clock = _setup(["x", "y"], {"x": 404, "y": 500})
    stats = await collect_all(conn, http, SETTINGS, cfg, clock)
    assert stats["failed"] == 1
    run = conn.execute("SELECT ok, error FROM source_runs WHERE source='greenhouse'").fetchone()
    assert run["ok"] == 0 and run["error"]
