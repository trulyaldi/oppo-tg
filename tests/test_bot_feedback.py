from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.handlers import handle_feedback
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
CFG = AppConfig.from_dict({})


class FakeMessage:
    def __init__(self):
        self.markups = []

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


class FakeCb:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, **kw):
        self.answers.append(text)


def labels(cb):
    return [b.text for b in cb.message.markups[-1].inline_keyboard[0]]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn):
    p = Posting(
        source="t",
        external_id="1",
        title="T",
        company="C",
        url="https://x.example/1",
        kind=Kind.JOB,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    return conn.execute("SELECT id FROM postings").fetchone()["id"]


def counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM reaction_log").fetchone()[0],
    )


async def test_like(conn):
    pid = add(conn)
    cb = FakeCb(f"fb:like:{pid}")
    await handle_feedback(cb, conn, CLOCK)
    assert db.active_reactions(conn, pid)["like"] is True
    assert len(cb.answers) == 1
    assert labels(cb) == ["✓ Like", "Dislike", "Save", "Applied"]


async def test_like_then_dislike(conn):
    pid = add(conn)
    await handle_feedback(FakeCb(f"fb:like:{pid}"), conn, CLOCK)
    cb = FakeCb(f"fb:dislike:{pid}")
    await handle_feedback(cb, conn, CLOCK)
    act = db.active_reactions(conn, pid)
    assert act["dislike"] and not act["like"]
    assert labels(cb) == ["Like", "✓ Dislike", "Save", "Applied"]


async def test_save_twice(conn):
    pid = add(conn)
    cb = FakeCb(f"fb:save:{pid}")
    await handle_feedback(cb, conn, CLOCK)
    await handle_feedback(cb, conn, CLOCK)
    assert db.active_reactions(conn, pid)["save"] is False
    assert conn.execute("SELECT COUNT(*) FROM reaction_log").fetchone()[0] == 2
    assert labels(cb) == ["Like", "Dislike", "Save", "Applied"]


@pytest.mark.parametrize("data", ["fb:nope:{pid}", "fb:like:", "fb:like:missing", "junk", None])
async def test_unknown(conn, data):
    pid = add(conn)
    cb = FakeCb(data.format(pid=pid) if data else None)
    await handle_feedback(cb, conn, CLOCK)
    assert cb.answers == ["Unknown action"]
    assert cb.message.markups == []
    assert counts(conn) == (0, 0)


async def test_persistence(tmp_path):
    path = tmp_path / "o.db"
    c1 = db.connect(path)
    db.migrate(c1)
    pid = add(c1)
    await handle_feedback(FakeCb(f"fb:applied:{pid}"), c1, CLOCK)
    c1.close()
    c2 = db.connect(path)
    assert db.active_reactions(c2, pid)["applied"] is True


def msg_update(chat_id):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="x"),
            text="hi",
        ),
    )


async def test_whitelist(conn):
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK)
    seen = []

    @dp.message()
    async def spy(message):
        seen.append(message.chat.id)

    bot = Bot("1:t")
    await dp.feed_update(bot, msg_update(99))
    await dp.feed_update(bot, Update(update_id=2))
    assert seen == []
    await dp.feed_update(bot, msg_update(42))
    assert seen == [42]
    await bot.session.close()


async def test_whitelist_blocks_callback(conn):
    pid = add(conn)
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK)
    bot = Bot("1:t")
    other = CallbackQuery(
        id="1",
        from_user=User(id=7, is_bot=False, first_name="x"),
        chat_instance="c",
        data=f"fb:like:{pid}",
        message=Message(message_id=1, date=NOW, chat=Chat(id=7, type="private"), text="m"),
    )
    await dp.feed_update(bot, Update(update_id=3, callback_query=other))
    assert counts(conn) == (0, 0)
    await bot.session.close()
