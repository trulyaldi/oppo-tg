from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.alerts import select_alerts, send_alerts
from src.delivery.format import Item, render_item
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)  # 14:00 Asia/Almaty
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
CFG = AppConfig.from_dict({})


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, score, tag, age_h=1, baseline=False, deadline=None):
    p = Posting(
        source="t",
        external_id=tag,
        title=f"Role {tag}",
        company="Acme",
        url="https://example.com/x",
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW - timedelta(hours=age_h))
    if baseline:
        conn.execute("UPDATE postings SET baseline=1 WHERE id=?", (p.id,))
    db.save_score(
        conn,
        p.id,
        ScoreResult(score=score, reason="ok", deadline=deadline, red_flags=[], tags=[]),
        "m",
        "v",
        NOW,
    )
    return p.id


def sel(conn, clock=CLOCK):
    return [(i.posting_id, k) for i, k in select_alerts(conn, CFG, clock)]


def test_fresh_threshold(conn):
    hit = add(conn, 7.6, "a")
    add(conn, 7.4, "b")
    assert sel(conn) == [(hit, "fresh_alert")]


def test_baseline_and_old_never_fresh_alert(conn):
    add(conn, 8.0, "a", baseline=True)
    add(conn, 8.0, "b", age_h=49)
    assert sel(conn) == []


def test_high_score_is_alert_and_uncapped(conn):
    big = add(conn, 9.2, "big")
    for i in range(6):
        add(conn, 7.6 + i * 0.1, f"f{i}")
    got = sel(conn)
    assert (big, "alert") in got
    assert sum(1 for _, k in got if k == "fresh_alert") == 5


async def test_cap_best_first_and_next_day(conn):
    ids = [add(conn, 7.5 + i * 0.1, f"f{i}") for i in range(6)]
    bot = FakeBot()
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 5
    kinds = {r["kind"] for r in conn.execute("SELECT kind FROM deliveries")}
    assert kinds == {"fresh_alert"}
    assert not db.is_delivered(conn, ids[0])  # the lowest is left for the digest
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 0
    nxt = FixedClock(NOW + timedelta(days=1))
    assert [p for p, _ in sel(conn, nxt)] == [ids[0]]
    assert await send_alerts(bot, conn, SETTINGS, CFG, nxt) == 1


async def test_alert_does_not_use_cap_and_paused(conn):
    add(conn, 9.2, "big")
    for i in range(5):
        add(conn, 7.6, f"f{i}")
    bot = FakeBot()
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 6
    assert sum(t.startswith("<b>NEW</b>") for t in bot.sent) == 5
    assert sum(t.startswith("<b>ALERT</b>") for t in bot.sent) == 1


def test_deadline_alert_unchanged(conn):
    pid = add(conn, 5.0, "d", deadline="2026-11-08")
    assert sel(conn) == [(pid, "alert")]


def test_render_fresh_header():
    item = Item("x", "T", "C", "https://e.com", 7.6, "r")
    assert render_item(item, alert=True, fresh=True).startswith("<b>NEW</b>\n")
    assert render_item(item, alert=True).startswith("<b>ALERT</b>\n")
    assert render_item(item).startswith("<b>T</b>")
