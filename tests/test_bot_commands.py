from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.bot.handlers import (
    cmd_applied,
    cmd_pause,
    cmd_resume,
    cmd_saved,
    cmd_sources,
    cmd_start,
    cmd_top,
)
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
ENV = {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
SETTINGS = Settings.from_env(ENV)
CFG = AppConfig.load(Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml")
FIXTURES = Path(__file__).parent / "fixtures"


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append((text, reply_markup, parse_mode))

    @property
    def texts(self):
        return [t for t, _, _ in self.sent]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, n, title=None, score=None, age_days=1, company="C"):
    p = Posting(
        source="t",
        external_id=str(n),
        title=title or f"Job {n}",
        company=company,
        url=f"https://x.example/{n}",
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    pid = conn.execute("SELECT id FROM postings WHERE external_id=?", (str(n),)).fetchone()["id"]
    if score is not None:
        db.save_score(
            conn,
            pid,
            ScoreResult(score=score, reason="why"),
            "m",
            "v",
            NOW - timedelta(days=age_days),
        )
        db.set_status(conn, pid, "scored")
    return pid


async def test_start_lists_commands():
    m = FakeMessage()
    await cmd_start(m)
    assert "/top" in m.texts[0] and "/sources" in m.texts[0]


async def test_top_ten_recent_highest_first(conn):
    for i in range(15):
        add(conn, i, score=5 + i * 0.1)
    for i in range(100, 102):
        add(conn, i, score=9.9, age_days=20)
    m = FakeMessage()
    await cmd_top(m, conn, CLOCK)
    assert len(m.sent) == 10
    scores = [float(t.split("\n")[1].split("/")[0]) for t in m.texts]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 6.4 and scores[-1] == 5.5
    assert not any("Job 100" in t or "Job 101" in t for t in m.texts)
    assert m.sent[0][1].inline_keyboard[0][0].callback_data.startswith("fb:like:")
    assert m.sent[0][2] == "HTML"


async def test_top_empty(conn):
    m = FakeMessage()
    await cmd_top(m, conn, CLOCK)
    assert m.texts == ["Nothing scored yet."]


async def test_top_escapes(conn):
    add(conn, 1, title="A <b>&", score=8)
    m = FakeMessage()
    await cmd_top(m, conn, CLOCK)
    assert "A &lt;b&gt;&amp;" in m.texts[0]
    assert "<b>A <b>" not in m.texts[0]


async def test_saved_applied_active_only_newest_first_max_20(conn):
    ids = [add(conn, i, title=f"S{i}") for i in range(25)]
    for i, pid in enumerate(ids):
        db.set_reaction(conn, pid, "save", NOW + timedelta(minutes=i))
    db.set_reaction(conn, ids[24], "save", NOW + timedelta(hours=1))  # toggled off
    db.set_reaction(conn, ids[3], "applied", NOW)
    m = FakeMessage()
    await cmd_saved(m, conn)
    lines = [ln for ln in m.texts[0].split("\n") if ln[:1].isdigit()]
    assert len(lines) == 20
    assert "S23" in lines[0] and "S24" not in m.texts[0]
    assert "S4</b>" in lines[19]
    a = FakeMessage()
    await cmd_applied(a, conn)
    assert "S3</b>" in a.texts[0] and "S2</b>" not in a.texts[0]


async def test_saved_applied_empty_and_escape(conn):
    m = FakeMessage()
    await cmd_saved(m, conn)
    await cmd_applied(m, conn)
    assert m.texts == ["Nothing saved yet.", "Nothing marked applied yet."]
    pid = add(conn, 1, title="X <i>", company="A&B")
    db.set_reaction(conn, pid, "save", NOW)
    m2 = FakeMessage()
    await cmd_saved(m2, conn)
    assert "X &lt;i&gt;" in m2.texts[0] and "A&amp;B" in m2.texts[0]


async def test_pause_toggle_and_resume(conn):
    m = FakeMessage()
    await cmd_pause(m, conn)
    assert db.get_state(conn, "paused") == "1"
    assert m.texts[-1] == "Paused. /pause again or /resume to resume."
    await cmd_pause(m, conn)
    assert db.get_state(conn, "paused") == "0"
    await cmd_pause(m, conn)
    await cmd_resume(m, conn)
    assert db.get_state(conn, "paused") == "0"


async def test_sources_exact_fixture(conn):
    rid = db.start_source_run(conn, "hn", NOW)
    db.finish_source_run(conn, rid, NOW, ok=True, n_items=201, n_new=5)
    rid = db.start_source_run(conn, "rss", NOW)
    db.finish_source_run(conn, rid, NOW, ok=False, error="Boom <x>")
    m = FakeMessage()
    await cmd_sources(m, conn, SETTINGS, CFG)
    assert len(m.sent) == 1
    assert m.sent[0][2] == "HTML"
    assert m.texts[0] == (FIXTURES / "sources_expected.txt").read_text().rstrip("\n")
