import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import db
from src.bot.handlers import START_TEXT, render_sources, render_stats
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
ENV = {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
SETTINGS = Settings.from_env(ENV)
FIXTURES = Path(__file__).parent / "fixtures"
CFG = AppConfig.load(FIXTURES / "config_repo_snapshot.toml")


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def seed(conn, source, ext, first_seen, *, target="", published=None):
    p = Posting(
        source=source,
        external_id=ext,
        title=f"T{ext}",
        company="C",
        url=f"https://x.example/{ext}",
        kind=Kind.JOB,
        published_at=published,
    )
    db.upsert_posting(conn, p, fingerprint(p), first_seen, target=target)
    return p.id


def test_latency_stored_and_logged(conn, caplog):
    caplog.set_level(logging.INFO)
    t0 = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
    pid = seed(conn, "greenhouse", "1", t0, target="acme")
    db.record_target_attempt(conn, "greenhouse", "acme", t0, ok=True, tier="tier1")
    assert db.record_delivery(conn, pid, "alert", "42", 1, t0 + timedelta(minutes=7))
    row = conn.execute("SELECT latency_s FROM deliveries").fetchone()
    assert row["latency_s"] == 420
    assert "delivery kind=alert source=greenhouse tier=tier1 latency_s=420" in caplog.text
    assert "T1" not in caplog.text


def test_stats_snapshot(conn):
    base = NOW - timedelta(days=1)
    lats = {"greenhouse": [60, 120, 300, 600, 3600], "hn": [900, 1800]}
    n = 0
    for src, vals in lats.items():
        for v in vals:
            n += 1
            pid = seed(conn, src, str(n), base, target="acme" if src == "greenhouse" else "")
            db.record_delivery(conn, pid, "digest", "42", n, base + timedelta(seconds=v))
    # fresh alert today (10:00Z is 15:00 Almaty, same local day)
    pid = seed(
        conn, "greenhouse", "fa", NOW - timedelta(minutes=30),
        target="acme", published=NOW - timedelta(hours=2),
    )  # fmt: skip
    db.record_delivery(conn, pid, "fresh_alert", "42", 99, NOW)
    # an old delivery outside the 7-day window
    old = NOW - timedelta(days=20)
    pid = seed(conn, "hn", "old", old)
    db.record_delivery(conn, pid, "digest", "42", 100, old + timedelta(hours=9))
    db.record_target_attempt(conn, "greenhouse", "acme", NOW, ok=True, tier="tier1")
    conn.execute("INSERT INTO llm_usage (day, purpose, calls) VALUES ('2026-11-06','score',12)")
    conn.execute("INSERT INTO llm_usage (day, purpose, calls) VALUES ('2026-11-06','draft',5)")
    conn.commit()
    out = render_stats(conn, SETTINGS, CFG, CLOCK)
    assert out == (FIXTURES / "stats_expected.txt").read_text().rstrip("\n")


def test_empty_stats(conn):
    out = render_stats(conn, SETTINGS, CFG, CLOCK)
    assert "Alerts: n/a" in out and "Digest: n/a" in out and "Source lag: n/a" in out
    assert "web" not in out.lower()


def test_sources_boards_per_tier(conn):
    db.record_target_attempt(conn, "greenhouse", "a", NOW, ok=True, tier="tier1")
    db.record_target_attempt(conn, "greenhouse", "b", NOW, ok=True, tier="tier1")
    db.record_target_attempt(conn, "greenhouse", "c", NOW, ok=True, tier="tier2")
    out = render_sources(conn, SETTINGS, CFG)
    assert "[ats] tier1 (boards: tier1 2, tier2 1) counted" in out


def test_start_lists_stats():
    assert "/stats" in START_TEXT


async def test_stats_other_chat_ignored(conn, monkeypatch):
    from aiogram import Bot

    from tests.test_bot_contacts import feed, upd

    calls = await feed(conn, None, upd(99, "/stats"), monkeypatch)
    assert calls == []
    calls = await feed(conn, None, upd(42, "/stats"), monkeypatch)
    assert any("Stats" in (getattr(c, "text", "") or "") for c in calls)
    assert Bot


def test_stats_triage_line_counts_today(conn):
    p1 = seed(conn, "hn", "t1", NOW)
    p2 = seed(conn, "hn", "t2", NOW)
    p3 = seed(conn, "hn", "t3", NOW)
    for pid, rel, at in (
        (p1, 1, NOW),
        (p2, 0, NOW),
        (p3, 1, NOW - timedelta(days=2)),  # not today
    ):
        conn.execute(
            "INSERT INTO triage_results VALUES (?,?,?,?,?,?,?)",
            (pid, "h", rel, "r", "m", "v", at.isoformat()),
        )
    conn.execute("UPDATE postings SET status='triage_error' WHERE id=?", (p3,))
    conn.execute("INSERT INTO llm_usage (day, purpose, calls) VALUES ('2026-11-06','triage',3)")
    conn.execute(
        "INSERT INTO llm_usage (day, purpose, calls) VALUES ('2026-11-06','triage_baseline',2)"
    )
    conn.commit()
    out = render_stats(conn, SETTINGS, CFG, CLOCK)
    assert "Triage today: yes 1, no 1, errors (all time) 1, calls 5/600" in out
