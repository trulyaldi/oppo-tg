import inspect
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.outreach_handlers import cmd_prepare, on_prepare_callback
from src.bot.refs import resolve_ref
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cv import store
from src.models import Kind, Posting
from src.outreach import prepare as prep

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_bait_fake.txt").read_text()
HONEST = (FX / "tailor_reply_honest.json").read_text()
BAIT = json.loads((FX / "posting_fabrication_bait.json").read_text())
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)


class Fake:
    def __init__(self, reply=HONEST, events=None):
        self.reply = reply
        self.calls = []
        self.events = events if events is not None else []
        self.messages = self

    async def create(self, **kw):
        from anthropic.resources.messages import AsyncMessages

        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        self.events.append("client")
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


class Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    prep.register_hooks()
    prep._locks.clear()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, ext="1", title=None):
    p = Posting(
        source="t", external_id=ext, title=title or BAIT["title"], company=BAIT["company"],
        url="https://example.com/j", description=BAIT["description"], kind=Kind.JOB,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    return p.id


def with_cv(conn):
    store.save_cv(conn, CV, json.dumps({"skills": ["Python"]}), "text", "m", NOW)


# --- resolve_ref ------------------------------------------------------------


def test_resolve_ref(conn):
    pid = add(conn)
    assert len(pid) == 16
    assert resolve_ref(conn, pid).posting_id == pid
    assert resolve_ref(conn, pid[:6].upper()).posting_id == pid
    assert "at least 6" in resolve_ref(conn, pid[:5]).error
    assert resolve_ref(conn, "abcdef0").error == "Posting not found."
    assert resolve_ref(conn, "zzzzzzzz").error == "Posting not found."
    assert resolve_ref(conn, "").error.startswith("Usage")


def test_resolve_ref_ambiguous_and_unknown(conn):
    for i, pid in enumerate(["abcdef0000000001", "abcdef0000000002", "123456abcdef0000"]):
        conn.execute(
            "INSERT INTO postings (id, source, external_id, title, url, fingerprint,"
            " first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (pid, "t", str(i), "T", "https://e.com", f"f{i}", "x", "x"),
        )
    assert "ambiguous" in resolve_ref(conn, "abcdef").error
    assert "ambiguous" in resolve_ref(conn, "abcdef00000000").error
    assert resolve_ref(conn, "abcdef0000000002").posting_id == "abcdef0000000002"
    assert resolve_ref(conn, "123456").posting_id == "123456abcdef0000"
    assert resolve_ref(conn, "fedcba").error == "Posting not found."


# --- handlers ---------------------------------------------------------------


async def test_no_cv(conn):
    pid = add(conn)
    client, m = Fake(), Msg()
    await cmd_prepare(m, conn, CLOCK, CFG, client, pid)
    assert m.sent == ["Upload your CV first with /cv."] and not client.calls


async def test_prepare_by_prefix_and_cached(conn):
    pid = add(conn, title="Quant <Intern> & co")
    with_cv(conn)
    client, m = Fake(), Msg()
    await cmd_prepare(m, conn, CLOCK, CFG, client, pid[:8])
    assert m.sent[0] == "Preparing Quant &lt;Intern&gt; &amp; co..."
    assert "Fit: partial" in m.sent[1] and len(client.calls) == 1
    m2 = Msg()
    await cmd_prepare(m2, conn, CLOCK, CFG, client, pid)
    assert len(client.calls) == 1 and "Fit: partial" in m2.sent[1]


async def test_prepare_bad_ref(conn):
    client, m = Fake(), Msg()
    await cmd_prepare(m, conn, CLOCK, CFG, client, "abc")
    assert "at least 6" in m.sent[0] and not client.calls


async def test_error_replies(conn):
    pid = add(conn)
    with_cv(conn)
    m = Msg()
    await cmd_prepare(m, conn, CLOCK, CFG, Fake(RuntimeError("boom")), pid)
    assert m.sent[-1] == "Could not prepare right now (api); try again later."
    assert conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0] == 0
    capped = AppConfig.from_dict({"outreach": {"daily_llm_call_cap": 0}})
    client, m2 = Fake(), Msg()
    await cmd_prepare(m2, conn, CLOCK, capped, client, pid)
    assert "Daily outreach LLM limit reached (0 calls)" in m2.sent[-1] and not client.calls
    m3 = Msg()
    await cmd_prepare(m3, conn, CLOCK, CFG, None, pid)
    assert "no LLM client" in m3.sent[-1]


async def test_callback_answers_before_client(conn):
    pid = add(conn)
    with_cv(conn)
    events = []
    client = Fake(events=events)
    m = Msg()

    class Cb:
        data = f"pr:{pid}"
        message = m

        async def answer(self, text=None, **kw):
            events.append("answer")

    await on_prepare_callback(Cb(), conn, CLOCK, CFG, client)
    assert events == ["answer", "client"]
    assert m.sent[0].startswith("Preparing ") and "Fit: partial" in m.sent[1]


async def test_callback_unknown_posting(conn):
    client, m = Fake(), Msg()

    class Cb:
        data = "pr:deadbeefdeadbeef"
        message = m

        async def answer(self, text=None, **kw):
            pass

    await on_prepare_callback(Cb(), conn, CLOCK, CFG, client)
    assert m.sent == ["Posting not found."] and not client.calls


# --- dispatcher: chat whitelist, commands, callback --------------------------


def msg_update(chat_id, text, uid=1):
    return Update(
        update_id=uid,
        message=Message(
            message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="x"), text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        ),
    )  # fmt: skip


def cb_update(chat_id, data):
    cb = CallbackQuery(
        id="1",
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
        chat_instance="c",
        data=data,
        message=Message(message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"), text="m"),
    )
    return Update(update_id=2, callback_query=cb)


async def feed(conn, client, update, monkeypatch):
    calls = []

    async def fake_call(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK, client)
    bot = Bot("1:t")
    await dp.feed_update(bot, update)
    await bot.session.close()
    return calls


def texts(calls):
    return [c.text for c in calls if getattr(c, "text", None)]


async def test_dispatcher_prepare_command_and_link_only_own_chat(conn, monkeypatch):
    pid = add(conn)
    with_cv(conn)
    client = Fake()
    assert await feed(conn, client, msg_update(99, f"/prepare {pid}"), monkeypatch) == []
    assert not client.calls
    calls = await feed(conn, client, msg_update(42, f"/prepare {pid}"), monkeypatch)
    sends = [c for c in calls if getattr(c, "text", None)]
    assert sends and all(str(c.chat_id) == SETTINGS.telegram_chat_id for c in sends)
    assert "Fit: partial" in sends[-1].text and len(client.calls) == 1
    calls = await feed(conn, client, msg_update(42, f"/prepare_{pid}"), monkeypatch)
    assert "Fit: partial" in texts(calls)[-1] and len(client.calls) == 1  # cached
    calls = await feed(conn, client, msg_update(42, "/prepare"), monkeypatch)
    assert texts(calls)[0].startswith("Usage")


async def test_dispatcher_prepare_callback(conn, monkeypatch):
    pid = add(conn)
    with_cv(conn)
    client = Fake()
    assert await feed(conn, client, cb_update(99, f"pr:{pid}"), monkeypatch) == []
    calls = await feed(conn, client, cb_update(42, f"pr:{pid}"), monkeypatch)
    assert type(calls[0]).__name__ == "AnswerCallbackQuery"
    assert "Fit: partial" in texts(calls)[-1] and len(client.calls) == 1


async def test_prepare_does_not_log_content(conn, caplog):
    pid = add(conn)
    with_cv(conn)
    caplog.set_level(logging.DEBUG)
    await cmd_prepare(Msg(), conn, CLOCK, CFG, Fake(), pid)
    assert BAIT["description"][:30] not in caplog.text and CV[:30] not in caplog.text
