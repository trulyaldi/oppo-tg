from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.alerts import select_alerts, send_alerts
from src.delivery.digest import select_digest, send_digest
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 5, 19, 30, tzinfo=UTC)  # 2026-11-06 00:30 Asia/Almaty
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
NOFRESH = {"fresh_alert_score": 10.0}  # these tests pin the base-score rules only
CFG = AppConfig.from_dict({"thresholds": NOFRESH})


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


def add(conn, score, deadline=None, title="Role <x>"):
    p = Posting(
        source="t",
        external_id=f"{score}-{deadline}",
        title=title,
        company="Acme",
        url="https://example.com/x",
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    db.save_score(
        conn,
        p.id,
        ScoreResult(score=score, reason="ok", deadline=deadline, red_flags=[], tags=[]),
        "m",
        "v",
        NOW,
    )
    return p.id


def sel(conn, cfg=CFG):
    return [i.posting_id for i, _ in select_alerts(conn, cfg, CLOCK)]


def test_score_threshold(conn):
    hi = add(conn, 9.0)
    add(conn, 8.9)
    assert sel(conn) == [hi]


def test_deadline_window(conn):
    near = add(conn, 4.0, "2026-11-13")
    add(conn, 4.1, "2026-11-14")
    add(conn, 4.2, "2026-11-05")
    today = add(conn, 4.3, "2026-11-06")
    assert set(sel(conn)) == {near, today}


def test_deadline_min_score_config(conn):
    add(conn, 4.0, "2026-11-13")
    cfg = AppConfig.from_dict({"thresholds": {"alert_deadline_min_score": 6.0}})
    assert sel(conn, cfg) == []


async def test_send_records_and_excludes_from_digest(conn):
    pid = add(conn, 9.5)
    bot = FakeBot()
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 1
    msg = bot.sent[0]
    assert msg["text"].startswith("<b>ALERT</b>")
    assert "Role &lt;x&gt;" in msg["text"]
    assert msg["parse_mode"] == "HTML" and msg["reply_markup"] is not None
    row = conn.execute("SELECT kind FROM deliveries WHERE posting_id=?", (pid,)).fetchone()
    assert row["kind"] == "alert"
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 0
    assert len(bot.sent) == 1
    assert select_digest(conn, CFG) == []
    await send_digest(bot, conn, SETTINGS, CFG, CLOCK)
    assert not any(m["text"].startswith("<b>Role") for m in bot.sent[1:])


async def test_paused_sends_nothing(conn):
    add(conn, 9.5)
    db.set_state(conn, "paused", "1")
    bot = FakeBot()
    assert await send_alerts(bot, conn, SETTINGS, CFG, CLOCK) == 0
    assert bot.sent == []
    assert conn.execute("SELECT COUNT(*) c FROM deliveries").fetchone()["c"] == 0


SNAP_W = str(Path(__file__).parent / "fixtures" / "weights_snapshot" / "weights.yaml")
WCFG = AppConfig.from_dict({"ranking": {"weights_file": SNAP_W}, "thresholds": NOFRESH})


def add_co(conn, company, base, deadline=None, region=None):
    p = Posting(
        source="t",
        external_id=f"{company}-{base}",
        title="Role",
        company=company,
        url="https://example.com/x",
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    db.save_score(
        conn,
        p.id,
        ScoreResult(
            score=base,
            reason="ok",
            deadline=deadline,
            employer_region=region or "unknown",
            region_confidence=0.9 if region else 0.0,
        ),
        "m",
        "v",
        NOW,
    )
    return p.id


def test_alerts_use_base_score(conn):
    hit = add_co(conn, "Acme Robotics", 9.0, region="intl")
    add_co(conn, "Citadel", 8.6)  # adjusted 9.6, base below 9.0
    items = [i for i, _ in select_alerts(conn, WCFG, CLOCK)]
    assert [i.posting_id for i in items] == [hit]
    assert items[0].flag == "\U0001f30d"
    assert items[0].score == 9.0 and items[0].base_score == 9.0


def test_deadline_alert_low_base(conn):
    pid = add_co(conn, "Arbuz.kz", 3.0, "2026-11-09")
    items = [i for i, _ in select_alerts(conn, WCFG, CLOCK)]
    assert [i.posting_id for i in items] == [pid]
    assert items[0].score == 4.0 and items[0].flag == "\U0001f1f0\U0001f1ff"
