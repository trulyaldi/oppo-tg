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
    report_scoring_outcome,
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
    assert is_total_failure({"scored": 0, "errors": 0, "skipped": 2})  # candidates, none scored
    assert not is_total_failure({"scored": 1, "errors": 0, "skipped": 2})


def test_message_is_clear_and_escaped():
    text = render_failure({"errors": 4}, "<b>bad</b> & worse", "claude-x")
    assert "ERROR: scoring failed" in text and "All 4 postings" in text
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
    assert "All 3 postings" in text and "TypeError" in text and "temperature" in text


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
    assert await report_scoring_outcome(bot, 1, conn, FixedClock(NOW), stats, "e", "m")
    soon = FixedClock(NOW + timedelta(hours=1))
    assert not await report_scoring_outcome(bot, 1, conn, soon, stats, "e", "m")
    later = FixedClock(NOW + REALERT_AFTER + timedelta(minutes=1))
    assert await report_scoring_outcome(bot, 1, conn, later, stats, "e", "m")
    assert len(bot.sent) == 2
    # a good run re-arms: the next outage alerts immediately
    await report_scoring_outcome(bot, 1, conn, later, {"scored": 5, "errors": 0}, None, "m")
    assert not db.get_state(conn, STATE_KEY)
    assert await report_scoring_outcome(bot, 1, conn, later, stats, "e", "m")
    assert len(bot.sent) == 3


async def test_telegram_failure_never_raises_and_is_retried_next_run(conn):
    stats = {"scored": 0, "errors": 2}
    ok = await report_scoring_outcome(FakeBot(fail=True), 1, conn, FixedClock(NOW), stats, "e", "m")
    assert ok is False
    assert not db.get_state(conn, STATE_KEY)  # not marked as sent
    bot = FakeBot()
    assert await report_scoring_outcome(bot, 1, conn, FixedClock(NOW), stats, "e", "m")
