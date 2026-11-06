import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db, main
from src.clock import FixedClock
from src.config import Settings
from src.models import Posting
from src.pipeline.dedupe import fingerprint
from src.scheduler import SchedulerDeps, build_scheduler, safe_job

TZ = ZoneInfo("Asia/Almaty")


def make(calls, **over):
    def rec(name):
        async def f():
            calls.append(name)

        return f

    kw = dict(
        clock=FixedClock(datetime(2026, 11, 6, 8, 0, tzinfo=TZ)),
        collect=rec("collect"),
        score=rec("score"),
        alerts=rec("alerts"),
        digest=rec("digest"),
        prune=rec("prune"),
    )
    kw.update(over)
    return build_scheduler(SchedulerDeps(**kw))


def test_jobs_and_policy():
    jobs = {j.id: j for j in make([]).get_jobs()}
    assert set(jobs) == {"collect_and_score", "daily_digest", "prune"}
    for j in jobs.values():
        assert j.coalesce is True
        assert j.max_instances == 1


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 11, 6, 8, 0, tzinfo=TZ), datetime(2026, 11, 6, 9, 0, tzinfo=TZ)),
        (datetime(2026, 11, 6, 9, 1, tzinfo=TZ), datetime(2026, 11, 7, 9, 0, tzinfo=TZ)),
    ],
)
def test_digest_next_fire(now, expected):
    trig = make([]).get_job("daily_digest").trigger
    nxt = trig.get_next_fire_time(None, now)
    assert nxt == expected
    assert nxt.utcoffset() == timedelta(hours=5)


async def test_collect_job_order_and_isolation():
    calls = []
    await make(calls).get_job("collect_and_score").func()
    assert calls == ["collect", "score", "alerts"]

    calls2 = []

    async def boom():
        calls2.append("collect")
        raise RuntimeError("x")

    await make(calls2, collect=boom).get_job("collect_and_score").func()
    assert calls2 == ["collect", "score", "alerts"]


async def test_safe_job_swallows(caplog):
    async def boom():
        raise ValueError("bad")

    with caplog.at_level(logging.ERROR):
        await safe_job("j", boom)()
    assert "job j failed" in caplog.text


def test_prune():
    now = datetime(2026, 11, 6, tzinfo=UTC)
    c = db.connect(":memory:")
    db.migrate(c)
    old = now - timedelta(days=61)
    new = now - timedelta(days=5)
    ids = {}
    for i, (status, seen) in enumerate(
        [("filtered", old), ("duplicate", old), ("scored", old), ("filtered", new)]
    ):
        p = Posting(
            source="t", external_id=str(i), title=f"T{i}", company="c", url=f"https://x/{i}"
        )
        db.upsert_posting(c, p, fingerprint(p), seen)
        row = c.execute("SELECT id FROM postings WHERE external_id=?", (str(i),)).fetchone()
        ids[i] = row["id"]
        db.set_status(c, row["id"], status)
    for table, col, key in (("http_cache", "url", "u"), ("robots_cache", "host", "h")):
        for suffix, at in (("old", now - timedelta(days=8)), ("new", now - timedelta(days=1))):
            c.execute(
                f"INSERT INTO {table}({col}, fetched_at) VALUES (?,?)",
                (key + suffix, at.isoformat()),
            )
    res = db.prune(c, now)
    assert res == {"http_cache": 1, "robots_cache": 1, "postings": 2}
    left = {r["id"] for r in c.execute("SELECT id FROM postings")}
    assert left == {ids[2], ids[3]}
    assert c.execute("SELECT count(*) FROM http_cache").fetchone()[0] == 1


class FakeScheduler:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def shutdown(self, wait=True):
        self.events.append("shutdown")


async def test_run_until_stopped_graceful():
    sched = FakeScheduler()
    state = {}

    async def polling():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    stop = asyncio.Event()
    task = asyncio.ensure_future(main.run_until_stopped(sched, polling, stop))
    await asyncio.sleep(0.01)
    assert sched.events == ["start"]
    stop.set()
    await task
    assert sched.events == ["start", "shutdown"]
    assert state["cancelled"]


async def test_polling_crash_does_not_stop_scheduler():
    sched = FakeScheduler()

    async def polling():
        raise RuntimeError("poll down")

    stop = asyncio.Event()
    task = asyncio.ensure_future(main.run_until_stopped(sched, polling, stop))
    await asyncio.sleep(0.01)
    assert not task.done()
    stop.set()
    await task
    assert sched.events == ["start", "shutdown"]


# --- main.py collect/once wiring of send_alerts (T23 follow-up) -------------


@pytest.mark.parametrize("command,digest_calls", [("collect", 0), ("once", 1)])
async def test_main_collect_once_wiring(monkeypatch, tmp_path, command, digest_calls):
    import aiogram
    import anthropic

    import src.delivery.alerts as alerts_mod

    order = []

    async def _aclose():
        order.append("bot_closed")

    class FakeBot:
        def __init__(self, token):
            self.session = type("S", (), {"close": staticmethod(_aclose)})()

    class FakeHttp:
        async def aclose(self):
            pass

    async def collect_all(*a, **k):
        order.append("collect")
        return {}

    async def score_pending(*a, **k):
        order.append("score")
        # total-failure alerts need the bot and the user's chat (D-83)
        assert isinstance(k["bot"], FakeBot) and k["chat_id"] == "1"
        return {}

    async def send_alerts(bot, conn, settings, cfg, clock):
        order.append("alerts")
        assert isinstance(bot, FakeBot)
        return 2

    async def send_digest(*a, **k):
        order.append("digest")
        return 0

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda api_key: object())
    monkeypatch.setattr(main, "build_client", lambda *a, **k: FakeHttp())
    monkeypatch.setattr(main, "collect_all", collect_all)
    monkeypatch.setattr(main, "score_pending", score_pending)
    monkeypatch.setattr(main, "send_digest", send_digest)
    monkeypatch.setattr(alerts_mod, "send_alerts", send_alerts)
    settings = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
            "ANTHROPIC_API_KEY": "k",
            "DB_PATH": str(tmp_path / "t.db"),
        }
    )
    await main._run(command, settings)
    expected = ["collect", "score", "alerts", "bot_closed"]
    if digest_calls:
        expected += ["digest", "bot_closed"]
    assert order == expected


def test_main_digest_missing_db_dir(monkeypatch, tmp_path):
    import aiogram
    import anthropic

    async def _aclose():
        pass

    class FakeBot:
        def __init__(self, token):
            self.session = type("S", (), {"close": staticmethod(_aclose)})()

    class FakeHttp:
        async def aclose(self):
            pass

    async def send_digest(*a, **k):
        return 0

    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda api_key: object())
    monkeypatch.setattr(main, "build_client", lambda *a, **k: FakeHttp())
    monkeypatch.setattr(main, "send_digest", send_digest)
    db_path = tmp_path / "nope" / "x.db"
    rc = main.main(
        ["digest"],
        environ={
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
            "ANTHROPIC_API_KEY": "k",
            "DB_PATH": str(db_path),
        },
    )
    assert rc == 0
    assert db_path.is_file()
