from datetime import UTC, datetime

import pytest

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.delivery.alerts import select_alerts
from src.delivery.digest import select_digest
from src.delivery.format import render_item
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint
from src.pipeline.filters import apply_filters

NOW = datetime(2026, 11, 5, 19, 30, tzinfo=UTC)
CLOCK = FixedClock(NOW)
FILTERS = {
    "positive_title_keywords": ["quant", "engineer", "intern"],
    "hard_blocker_keywords": ["citizens only", "security clearance"],
}


def cfg(**extra):
    return AppConfig.from_dict(
        {"thresholds": {"fresh_alert_score": 10.0}, "filters": {**FILTERS, **extra}}
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, title, desc, base=9.5):
    p = Posting(
        source="t",
        external_id=title,
        title=title,
        company="Acme",
        url="https://example.com/x",
        description=desc,
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    return p.id


def score(conn, pid, base=9.5):
    db.save_score(
        conn, pid, ScoreResult(score=base, reason="ok", red_flags=[], tags=[]), "m", "v", NOW
    )


def test_lower_mode_no_alert_penalty_and_flag(conn):
    c = cfg()
    pid = add(conn, "Quant Engineer", "Open to US citizens only.")
    apply_filters(conn, c, CLOCK)
    assert conn.execute("SELECT hard_blocker FROM postings").fetchone()[0] == "citizens only"
    score(conn, pid)
    assert select_alerts(conn, c, CLOCK) == []
    row = conn.execute("SELECT * FROM scores").fetchone()
    assert row["base_score"] == 9.5
    assert row["adjusted_score"] == 7.5
    assert "hard blocker" in row["adjustment_detail"]
    items = select_digest(conn, c, clock=CLOCK)
    assert [i.posting_id for i in items] == [pid]
    assert "Hard blocker: citizens only" in render_item(items[0])


def test_clean_posting_still_alerts(conn):
    c = cfg()
    pid = add(conn, "Quant Engineer", "Great role.")
    apply_filters(conn, c, CLOCK)
    score(conn, pid)
    assert [i.posting_id for i, _ in select_alerts(conn, c, CLOCK)] == [pid]


def test_penalty_clamped_at_zero(conn):
    c = cfg(hard_blocker_penalty=5.0)
    pid = add(conn, "Quant Engineer", "citizens only")
    apply_filters(conn, c, CLOCK)
    score(conn, pid, base=3.0)
    select_alerts(conn, c, CLOCK)
    assert conn.execute("SELECT adjusted_score FROM scores").fetchone()[0] == 0.0


def test_filter_mode_filters_before_scoring(conn):
    c = cfg(hard_blocker_action="filter")
    add(conn, "Quant Engineer", "US citizens only")
    counts = apply_filters(conn, c, CLOCK)
    assert counts == {"hard_blocker": 1}
    row = conn.execute("SELECT status, filter_reason FROM postings").fetchone()
    assert (row["status"], row["filter_reason"]) == ("filtered", "hard_blocker")
    assert db.pending_for_scoring(conn, 10) == []


def test_title_detected_and_phrase_match_only(conn):
    c = cfg()
    add(conn, "Quant Engineer (Security Clearance)", "")
    add(conn, "Engineer", "Join our clearance sale team")
    apply_filters(conn, c, CLOCK)
    rows = {
        r["title"]: r["hard_blocker"]
        for r in conn.execute("SELECT title, hard_blocker FROM postings")
    }
    assert rows["Quant Engineer (Security Clearance)"] == "security clearance"
    assert rows["Engineer"] is None
