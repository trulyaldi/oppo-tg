"""Baseline mode (D-188): a target's first ok fetch is stored as baseline, never alerted."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from src import db as dbm
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.alerts import select_alerts
from src.delivery.digest import select_digest
from src.http import build_client
from src.models import ScoreResult
from src.pipeline.run import collect_all

NOW = datetime(2026, 11, 5, 19, 30, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_jobs.json").read_text())


class Env:
    def __init__(self, boards):
        self.cfg = AppConfig.from_dict(
            {
                "sources": {"greenhouse": {"enabled": True, "boards": boards}},
                "filters": {"positive_title_keywords": ["a"]},
            }
        )
        self.conn = dbm.connect(":memory:")
        dbm.migrate(self.conn)
        self.status: dict[str, int] = {}
        self.extra: dict[str, list[dict]] = {}

        def handler(req):
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text="")
            board = req.url.path.split("/")[3]
            st = self.status.get(board, 200)
            if st != 200:
                return httpx.Response(st, text="x")
            body = dict(FIXTURE)
            body["jobs"] = FIXTURE["jobs"] + self.extra.get(board, [])
            return httpx.Response(200, json=body)

        async def nosleep(_):
            pass

        self.http = build_client(
            self.conn, CLOCK, transport=httpx.MockTransport(handler), sleep=nosleep
        )

    async def collect(self):
        self.conn.execute("DELETE FROM http_cache")  # each collect re-fetches
        return await collect_all(self.conn, self.http, SETTINGS, self.cfg, CLOCK)

    def baselines(self, board):
        rows = self.conn.execute(
            "SELECT baseline FROM postings WHERE target=? ORDER BY rowid", (board,)
        ).fetchall()
        return [r["baseline"] for r in rows]


async def _nosleep(_):
    pass


def new_job():
    j = dict(FIXTURE["jobs"][0])
    j["id"] = 987654321
    j["title"] = "Brand New Research Intern"
    return j


def score_all(conn, score=9.5, deadline="2026-11-08"):
    for r in conn.execute("SELECT id FROM postings").fetchall():
        dbm.save_score(
            conn,
            r["id"],
            ScoreResult(
                score=score,
                reason="ok",
                deadline=datetime.fromisoformat(deadline).date() if deadline else None,
                red_flags=[],
                tags=[],
            ),
            "m",
            "v",
            NOW,
        )


def alerts(env):
    return [i.posting_id for i in select_alerts(env.conn, env.cfg, CLOCK)]


async def test_first_fetch_is_baseline_and_not_alerted():
    env = Env(["a"])
    await env.collect()
    assert env.baselines("a") == [1] * len(FIXTURE["jobs"])
    assert dbm.get_target_state(env.conn, "greenhouse", "a")["baselined_at"]
    score_all(env.conn)
    assert alerts(env) == []


async def test_second_fetch_new_job_is_not_baseline_and_alerts():
    env = Env(["a"])
    await env.collect()
    env.extra["a"] = [new_job()]
    await env.collect()
    flags = env.baselines("a")
    assert flags[:-1] == [1] * len(FIXTURE["jobs"]) and flags[-1] == 0
    score_all(env.conn)
    got = alerts(env)
    assert len(got) == 1
    row = env.conn.execute("SELECT title FROM postings WHERE id=?", (got[0],)).fetchone()
    assert row["title"] == "Brand New Research Intern"


async def test_failed_first_fetch_then_success_baselines_on_success():
    env = Env(["a"])
    env.status["a"] = 500
    await env.collect()
    assert dbm.get_target_state(env.conn, "greenhouse", "a")["baselined_at"] is None
    env.status["a"] = 200
    await env.collect()
    assert env.baselines("a") == [1] * len(FIXTURE["jobs"])
    assert dbm.get_target_state(env.conn, "greenhouse", "a")["baselined_at"]


async def test_new_board_baselines_only_itself():
    env = Env(["a"])
    await env.collect()
    env.cfg = AppConfig.from_dict(
        {
            "sources": {"greenhouse": {"enabled": True, "boards": ["a", "b"]}},
            "filters": {"positive_title_keywords": ["a"]},
        }
    )
    env.extra["a"] = [new_job()]
    env.extra["b"] = [new_job()]
    await env.collect()
    assert env.baselines("a")[-1] == 0
    assert env.baselines("b") == [1] * (len(FIXTURE["jobs"]) + 1)


async def test_migrated_v7_existing_board_new_job_not_baseline(tmp_path):
    import sqlite3

    path = tmp_path / "v7.db"
    c = sqlite3.connect(str(path))
    for s in dbm._MIGRATIONS[:7]:
        for stmt in s.split(";"):
            if stmt.strip():
                c.execute(stmt)
    c.execute("PRAGMA user_version=7")
    c.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint, status,"
        " first_seen_at, last_seen_at) VALUES ('old','greenhouse','a:1','T','https://x/1','fp1',"
        "'scored','2026-11-01T00:00:00+06:00','2026-11-02T00:00:00+06:00')"
    )
    c.commit()
    c.close()
    env = Env(["a"])
    env.conn = dbm.connect(str(path))
    dbm.migrate(env.conn)
    env.http = build_client(
        env.conn,
        CLOCK,
        sleep=_nosleep,
        transport=httpx.MockTransport(
            lambda req: (
                httpx.Response(200, text="")
                if req.url.path == "/robots.txt"
                else httpx.Response(200, json={"jobs": [new_job()]})
            )
        ),
    )
    await env.collect()
    row = env.conn.execute("SELECT baseline FROM postings WHERE external_id LIKE 'a:%9876%'")
    assert [r["baseline"] for r in row.fetchall()] == [0]


async def test_baseline_items_can_appear_in_digest():
    env = Env(["a"])
    await env.collect()
    score_all(env.conn, score=8.0, deadline=None)
    items = select_digest(env.conn, env.cfg)
    assert len(items) > 0


async def test_scoring_picks_non_baseline_first():
    env = Env(["a"])
    await env.collect()
    env.extra["a"] = [new_job()]
    await env.collect()
    env.conn.execute("UPDATE postings SET status='new', filter_reason=NULL")  # past staleness
    rows = dbm.pending_for_scoring(env.conn, 3)
    assert rows[0]["baseline"] == 0 and rows[1]["baseline"] == 1
