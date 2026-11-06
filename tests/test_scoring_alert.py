import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db
from src.clock import FixedClock
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.pipeline.run import score_pending
from src.pipeline.scoring_alert import (
    REALERT_AFTER,
    STATE_KEY,
    is_total_failure,
    render_failure,
    report_stage_outcome,
)

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
GOOD = json.dumps({"score": 8.0, "reason": "ok", "deadline": None, "red_flags": [], "tags": []})
BOOM = TypeError("create() got an unexpected keyword argument 'temperature'")


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, text, kw))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.messages = self

    async def create(self, **kw):
        r = self.replies.pop(0) if self.replies else GOOD
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, n):
    for i in range(n):
        p = Posting(
            source="t",
            external_id=str(i),
            title=f"Quant intern {i}",
            company="Acme",
            url="https://example.com/x",
            kind=Kind.INTERNSHIP,
        )
        db.upsert_posting(conn, p, fingerprint(p), NOW)


def test_is_total_failure():
    assert is_total_failure({"scored": 0, "errors": 3})
    assert not is_total_failure({"scored": 1, "errors": 3})  # partial failure
    assert not is_total_failure({"scored": 0, "errors": 0})  # nothing to score
    assert not is_total_failure({"scored": 0, "errors": 0, "skipped": 2})  # skipped is no failure
    assert not is_total_failure({"scored": 0, "errors": 0, "capped": 5})  # cap is no failure
    assert not is_total_failure({"scored": 1, "errors": 0, "skipped": 2})


def test_message_is_clear_and_escaped():
    text = render_failure({"errors": 4}, "<b>bad</b> & worse", "claude-x")
    assert "ERROR: scoring failed" in text and "All 4 scoring attempts" in text
    assert "claude-x" in text and "ANTHROPIC_API_KEY" in text
    assert "<b>bad</b>" not in text and "&lt;b&gt;bad&lt;/b&gt; &amp; worse" in text


async def test_total_failure_sends_one_telegram_message(conn):
    add(conn, 3)
    bot = FakeBot()
    stats = await score_pending(
        conn, FakeClient([BOOM] * 10), "m", FixedClock(NOW), 100, bot=bot, chat_id=42
    )
    assert stats["errors"] == 3 and stats["scored"] == 0
    assert len(bot.sent) == 1
    chat_id, text, kw = bot.sent[0]
    assert chat_id == 42 and kw["parse_mode"] == "HTML"
    assert "All 3 scoring attempts" in text and "TypeError" in text and "temperature" in text


async def test_no_message_when_something_scored(conn):
    add(conn, 3)
    bot = FakeBot()
    await score_pending(
        conn, FakeClient([BOOM, GOOD, GOOD]), "m", FixedClock(NOW), 100, bot=bot, chat_id=42
    )
    assert bot.sent == []


async def test_no_message_when_nothing_pending(conn):
    bot = FakeBot()
    await score_pending(conn, FakeClient([]), "m", FixedClock(NOW), 100, bot=bot, chat_id=42)
    assert bot.sent == []


async def test_works_without_a_bot(conn):
    add(conn, 1)
    stats = await score_pending(conn, FakeClient([BOOM] * 5), "m", FixedClock(NOW), 100)
    assert stats["errors"] == 1


async def test_throttled_then_realerts_then_rearms_on_success(conn):
    bot = FakeBot()
    stats = {"scored": 0, "errors": 2}
    assert await report_stage_outcome("scoring", bot, 1, conn, FixedClock(NOW), stats, "e", "m")
    soon = FixedClock(NOW + timedelta(hours=1))
    assert not await report_stage_outcome("scoring", bot, 1, conn, soon, stats, "e", "m")
    later = FixedClock(NOW + REALERT_AFTER + timedelta(minutes=1))
    assert await report_stage_outcome("scoring", bot, 1, conn, later, stats, "e", "m")
    assert len(bot.sent) == 2
    # a good run re-arms: the next outage alerts immediately
    await report_stage_outcome(
        "scoring", bot, 1, conn, later, {"scored": 5, "errors": 0}, None, "m"
    )
    assert not db.get_state(conn, STATE_KEY)
    assert await report_stage_outcome("scoring", bot, 1, conn, later, stats, "e", "m")
    assert len(bot.sent) == 3


async def test_telegram_failure_never_raises_and_is_retried_next_run(conn):
    stats = {"scored": 0, "errors": 2}
    ok = await report_stage_outcome(
        "scoring", FakeBot(fail=True), 1, conn, FixedClock(NOW), stats, "e", "m"
    )
    assert ok is False
    assert not db.get_state(conn, STATE_KEY)  # not marked as sent
    bot = FakeBot()
    assert await report_stage_outcome("scoring", bot, 1, conn, FixedClock(NOW), stats, "e", "m")


def set_baseline(conn, n):
    ids = [r[0] for r in conn.execute("SELECT id FROM postings ORDER BY external_id").fetchall()]
    for pid in ids[:n]:
        conn.execute("UPDATE postings SET baseline=1 WHERE id=?", (pid,))
    conn.commit()


def calls_today(conn, *purposes):
    q = ",".join("?" * len(purposes))
    return conn.execute(
        f"SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE purpose IN ({q})", purposes
    ).fetchone()[0]


async def test_daily_cap_across_runs_then_resumes_next_day(conn):
    from src.config import ScoringConfig

    add(conn, 12)
    cfg = ScoringConfig(daily_call_cap=5)
    bot = FakeBot()
    clock = FixedClock(NOW)
    client = FakeClient([])
    for _ in range(2):
        await score_pending(conn, client, "m", clock, 100, bot=bot, chat_id=1, scoring_cfg=cfg)
    assert calls_today(conn, "score", "score_baseline") == 5
    assert len(bot.sent) == 1  # one warning per day, not per run
    assert "daily_call_cap" in bot.sent[0][1] and "5" in bot.sent[0][1]
    clock.set(NOW + timedelta(days=1))
    stats = await score_pending(conn, client, "m", clock, 100, bot=bot, chat_id=1, scoring_cfg=cfg)
    assert stats["scored"] == 5
    assert len(bot.sent) == 2  # a new day warns again


async def test_baseline_subcap_leaves_room_for_new_postings(conn):
    from src.config import ScoringConfig

    add(conn, 30)
    set_baseline(conn, 30)
    cfg = ScoringConfig(daily_call_cap=10, baseline_max_share=0.5)
    clock = FixedClock(NOW)
    client = FakeClient([])
    await score_pending(conn, client, "m", clock, 100, scoring_cfg=cfg)
    assert calls_today(conn, "score_baseline") == 5
    p = Posting(
        source="t", external_id="late", title="Quant intern late", company="Acme",
        url="https://example.com/y", kind=Kind.INTERNSHIP,
    )  # fmt: skip
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    stats = await score_pending(conn, client, "m", clock, 100, scoring_cfg=cfg)
    assert stats["scored"] == 1 and calls_today(conn, "score") == 1
    assert calls_today(conn, "score_baseline") == 5


async def test_cap_only_run_is_not_a_failure_notice(conn):
    from src.config import ScoringConfig

    add(conn, 3)
    bot = FakeBot()
    stats = await score_pending(
        conn, FakeClient([]), "m", FixedClock(NOW), 100, bot=bot, chat_id=1,
        scoring_cfg=ScoringConfig(daily_call_cap=0),
    )  # fmt: skip
    assert stats["capped"] == 3 and not is_total_failure(stats)
    assert all("ERROR: scoring failed" not in t for _, t, _ in bot.sent)
    assert len(bot.sent) == 1 and "Scoring cap reached" in bot.sent[0][1]


async def test_stage_throttle_keys_are_independent_and_triage_names_its_model(conn):
    bot = FakeBot()
    stats = {"scored": 0, "errors": 3, "capped": 0}
    clock = FixedClock(NOW)
    assert await report_stage_outcome("triage", bot, 1, conn, clock, stats, "e<", "haiku-x")
    assert not await report_stage_outcome("triage", bot, 1, conn, clock, stats, "e", "haiku-x")
    assert await report_stage_outcome("scoring", bot, 1, conn, clock, stats, "e", "m")
    text = bot.sent[0][1]
    assert "ERROR: triage failed" in text and "haiku-x" in text and "e&lt;" in text
    assert "[triage] model" in text and "SCORING_MODEL" not in text
    assert "SCORING_MODEL" in bot.sent[1][1]
    # a triage success re-arms only triage
    await report_stage_outcome("triage", bot, 1, conn, clock, {"scored": 2, "errors": 0}, None, "h")
    assert await report_stage_outcome("triage", bot, 1, conn, clock, stats, "e", "haiku-x")
    assert not await report_stage_outcome("scoring", bot, 1, conn, clock, stats, "e", "m")


async def test_cap_alone_never_alerts_any_stage(conn):
    bot = FakeBot()
    stats = {"scored": 0, "errors": 0, "capped": 5}
    for stage in ("triage", "scoring"):
        assert not await report_stage_outcome(
            stage, bot, 1, conn, FixedClock(NOW), stats, None, "m"
        )
    assert all("cap reached" in m[1] for m in bot.sent)
