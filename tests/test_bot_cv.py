import inspect
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.handlers import START_TEXT
from src.bot.outreach_handlers import (
    NO_SECTIONS,
    TOO_SHORT,
    cmd_cancel,
    cmd_cv,
    cmd_forgetcv,
    on_text,
)
from src.bot.pending import peek_pending, set_pending, take_pending
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cv.store import current_cv

FIX = Path(__file__).parent / "fixtures"
CV = (FIX / "cv_fake.txt").read_text()
REPLY = (FIX / "cv_parse_reply.json").read_text()
CFG = AppConfig.load(FIX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)


class FakeClient:
    def __init__(self, reply=REPLY):
        self.reply = reply
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        from anthropic.resources.messages import AsyncMessages

        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def answer(self, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append(text)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def clock():
    return FixedClock(NOW)


async def start_cv(conn, clock):
    m = Msg()
    await cmd_cv(m, conn, clock, CFG)
    return m


async def test_cv_prompt(conn, clock):
    m = await start_cv(conn, clock)
    assert "Anthropic API" in m.sent[0] and "data/" in m.sent[0]
    assert peek_pending(conn, NOW).kind == "cv"


async def test_ingest_unchanged_and_forget(conn, clock):
    client = FakeClient()
    await start_cv(conn, clock)
    m = Msg(CV)
    await on_text(m, conn, clock, CFG, client)
    assert "CV v1 saved" in m.sent[0] and "sent to the Anthropic API" in m.sent[0]
    assert "education 1" in m.sent[0]
    assert current_cv(conn)["version"] == 1
    assert peek_pending(conn, NOW) is None
    n = len(client.calls)
    await start_cv(conn, clock)
    m2 = Msg(CV)
    await on_text(m2, conn, clock, CFG, client)
    assert "unchanged (v1)" in m2.sent[0]
    assert len(client.calls) == n
    m3 = Msg()
    await cmd_forgetcv(m3, conn, clock)
    assert "deleted" in m3.sent[0] and current_cv(conn) is None
    m4 = Msg()
    await cmd_forgetcv(m4, conn, clock)
    assert m4.sent == ["No CV stored."]


async def test_too_short_keeps_pending(conn, clock):
    client = FakeClient()
    await start_cv(conn, clock)
    m = Msg("short")
    await on_text(m, conn, clock, CFG, client)
    assert m.sent == [TOO_SHORT] and not client.calls
    assert peek_pending(conn, NOW) is not None


async def test_no_sections(conn, clock):
    await start_cv(conn, clock)
    m = Msg(CV)
    await on_text(m, conn, clock, CFG, FakeClient('{"education": [], "experience": []}'))
    assert m.sent == [NO_SECTIONS] and current_cv(conn) is None


async def test_cancel(conn, clock):
    await start_cv(conn, clock)
    m = Msg()
    await cmd_cancel(m, conn)
    assert m.sent == ["Cancelled."] and peek_pending(conn, NOW) is None
    m2 = Msg()
    await cmd_cancel(m2, conn)
    assert m2.sent == ["Nothing to cancel."]


def test_pending_expiry(conn):
    set_pending(conn, "cv", None, NOW, 15)
    assert peek_pending(conn, NOW + timedelta(minutes=14)) is not None
    assert take_pending(conn, NOW + timedelta(minutes=16)) is None
    assert take_pending(conn, NOW) is None  # cleared


def msg_update(chat_id, text):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="x"),
            text=text,
        ),
    )


async def run_dispatch(conn, clock, client, chat_id, text, monkeypatch):
    sent = []

    async def fake_call(self, method, *a, **kw):
        sent.append(getattr(method, "text", None))
        return None

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, SETTINGS, CFG, clock, client)
    bot = Bot("1:t")
    await dp.feed_update(bot, msg_update(chat_id, text))
    await bot.session.close()
    return sent


async def test_dispatcher_ignores_without_pending_and_expired(conn, clock, monkeypatch):
    client = FakeClient()
    assert await run_dispatch(conn, clock, client, 42, CV, monkeypatch) == []
    set_pending(conn, "cv", None, NOW, 15)
    clock.set(NOW + timedelta(minutes=16))
    assert await run_dispatch(conn, clock, client, 42, CV, monkeypatch) == []
    assert not client.calls and current_cv(conn) is None


async def test_dispatcher_ingests_and_other_chat_not_consumed(conn, clock, monkeypatch):
    client = FakeClient()
    set_pending(conn, "cv", None, NOW, 15)
    assert await run_dispatch(conn, clock, client, 99, CV, monkeypatch) == []
    assert not client.calls and peek_pending(conn, NOW) is not None
    out = await run_dispatch(conn, clock, client, 42, CV, monkeypatch)
    assert len(out) == 1 and "CV v1 saved" in out[0]


async def test_dispatcher_cv_command(conn, clock, monkeypatch):
    out = await run_dispatch(conn, clock, FakeClient(), 42, "/cv", monkeypatch)
    assert "Anthropic API" in out[0]


async def test_canary_not_logged(conn, clock, caplog):
    caplog.set_level(logging.DEBUG)
    text = CV + "\nCANARY-CV-TEXT extra line for the canary check\n"
    reply = REPLY
    await start_cv(conn, clock)
    await on_text(Msg(text), conn, clock, CFG, FakeClient(reply))
    assert current_cv(conn) is not None
    assert "CANARY-CV-TEXT" not in caplog.text
    # failing path too
    await start_cv(conn, clock)
    await on_text(Msg(text + "x"), conn, clock, CFG, FakeClient("not json"))
    assert "CANARY-CV-TEXT" not in caplog.text


def test_start_lists_commands():
    for c in ("/cv", "/forgetcv", "/cancel"):
        assert c in START_TEXT


async def test_new_version_reports_cleared_cache(conn, clock):
    client = FakeClient()
    await start_cv(conn, clock)
    await on_text(Msg(CV), conn, clock, CFG, client)
    conn.execute(
        "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
        " first_seen_at, last_seen_at) VALUES ('p1','t','1','T','https://e.com','f','x','x')"
    )
    conn.execute("INSERT INTO tailoring_cache VALUES ('p1', 1, '{}', 0, 'm', 'v', 'x')")
    conn.commit()
    await start_cv(conn, clock)
    m = Msg(CV + "\nExtra achievement line FAKE FIXTURE")
    await on_text(m, conn, clock, CFG, client)
    assert "CV v2 saved" in m.sent[0]
    assert "Cleared 1 cached results for older CV versions." in m.sent[0]
