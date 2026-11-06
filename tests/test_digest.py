from datetime import UTC, datetime

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.digest import EMPTY_TEXT, select_digest, send_digest
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
CFG = AppConfig.from_dict({})


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return type("M", (), {"message_id": 100 + len(self.sent)})()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add_scored(conn, scores):
    ids = []
    for i, s in enumerate(scores):
        p = Posting(
            source="t",
            external_id=str(i),
            title=f"Role {i}",
            company="Acme",
            url="https://example.com/x",
            kind=Kind.JOB,
        )
        db.upsert_posting(conn, p, fingerprint(p), NOW)
        db.save_score(
            conn,
            p.id,
            ScoreResult(score=s, reason="ok", deadline=None, red_flags=[], tags=[]),
            "m",
            "v",
            NOW,
        )
        ids.append(p.id)
    return ids


def test_select_threshold_max_and_order(conn):
    add_scored(conn, [3.0, 3.5, 4.9, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.4])
    items = select_digest(conn, CFG)
    assert len(items) == 10
    scores = [i.score for i in items]
    assert scores == sorted(scores, reverse=True)
    assert min(scores) >= 5.0 and scores[0] == 9.4


def test_select_excludes_delivered(conn):
    ids = add_scored(conn, [8.0, 7.0])
    db.record_delivery(conn, ids[0], "alert", "42", 1, NOW)
    assert [i.posting_id for i in select_digest(conn, CFG)] == [ids[1]]


async def test_send_records_deliveries(conn):
    ids = add_scored(conn, [8.0, 7.0, 2.0])
    bot = FakeBot()
    n = await send_digest(bot, conn, SETTINGS, CFG, FixedClock(NOW))
    assert n == 2 and len(bot.sent) == 3
    assert "Daily digest 2026-11-06" in bot.sent[0]["text"] and "2 items" in bot.sent[0]["text"]
    assert all(m["parse_mode"] == "HTML" and m["chat_id"] == "42" for m in bot.sent)
    assert all(db.is_delivered(conn, i) for i in ids[:2]) and not db.is_delivered(conn, ids[2])
    row = conn.execute("SELECT * FROM deliveries WHERE posting_id=?", (ids[0],)).fetchone()
    assert row["kind"] == "digest" and row["message_id"] == 102


async def test_paused_sends_nothing(conn):
    add_scored(conn, [8.0])
    db.set_state(conn, "paused", "1")
    bot = FakeBot()
    assert await send_digest(bot, conn, SETTINGS, CFG, FixedClock(NOW)) == 0
    assert bot.sent == []
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


async def test_empty_day_message(conn):
    add_scored(conn, [3.0, 4.0])
    bot = FakeBot()
    assert await send_digest(bot, conn, SETTINGS, CFG, FixedClock(NOW)) == 0
    assert [m["text"] for m in bot.sent] == [EMPTY_TEXT]
    assert EMPTY_TEXT == "No new opportunities today."


async def test_item_send_failure_not_recorded(conn):
    ids = add_scored(conn, [8.0, 7.0])

    class Flaky(FakeBot):
        async def send_message(self, chat_id, text, **kw):
            if "Role 0" in text:
                raise RuntimeError("boom")
            return await super().send_message(chat_id, text, **kw)

    n = await send_digest(Flaky(), conn, SETTINGS, CFG, FixedClock(NOW))
    assert n == 1 and not db.is_delivered(conn, ids[0]) and db.is_delivered(conn, ids[1])
