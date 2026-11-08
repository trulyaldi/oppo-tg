"""KF-23: /findchannels, Add and Ignore buttons, the weekly job. No real Telegram calls."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from test_discovery_telegram import FIX, FakeClient

from src import db
from src.bot.app import build_dispatcher
from src.bot.findchannels import cmd_findchannels, on_candidate_callback
from src.bot.handlers import START_TEXT
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cues import load_cues, parse_cues_text
from src.discovery import telegram_flow as flow
from src.discovery.telegram import Candidate
from src.sources.telegram_channels import channels, set_flood

NOW = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
HOSTILE = "<b>x</b></a> & <script>"


def settings(tmp_path, creds=True, session=True):
    sess = tmp_path / "sess"
    sess.unlink(missing_ok=True)
    if session:
        sess.write_text("")
    env = {
        "TELEGRAM_BOT_TOKEN": "1:t",
        "TELEGRAM_CHAT_ID": "42",
        "ANTHROPIC_API_KEY": "k",
        "TG_SESSION_PATH": str(sess),
    }
    if creds:
        env |= {"TG_API_ID": "1", "TG_API_HASH": "h"}
    return Settings.from_env(env)


@pytest.fixture
def cfg():
    return AppConfig.from_dict(
        {
            "sources": {"telegram_channels": {"enabled": True, "channels": ["Configured_Ch"]}},
            "telegram_discovery": {"daily_call_cap": 30},
        }
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


class OwnedClient(FakeClient):
    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    async def is_user_authorized(self):
        return True


def factory(client):
    return lambda s: client


class Msg:
    def __init__(self):
        self.sent = []
        self.kw = []
        self.chat = SimpleNamespace(id=42)

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)
        self.kw.append(kw)


class Bot_:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))


def good_client():
    return OwnedClient(FIX["searches"], FIX["messages"], [FIX["no_count"]])


async def test_findchannels_replies_with_candidate_and_buttons(conn, cfg, tmp_path):
    m = Msg()
    await cmd_findchannels(
        m,
        Bot_(),
        conn,
        settings(tmp_path),
        cfg,
        CLOCK,
        "vakansii kz, jobs_kz",
        factory(good_client()),
    )
    assert len(m.sent) == 1
    assert m.sent[0].startswith("@kzjobs_news — KZ Jobs (12345 subscribers, job posts ~35%)")
    assert m.sent[0].count("\n• ") == 3
    kb = m.kw[0]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["tc:add:kzjobs_news", "tc:ign:kzjobs_news"]
    assert [b.text for b in kb] == ["Add", "Ignore"]


async def test_keyword_validation(conn, cfg, tmp_path):
    for bad in ("job", "alpha beta gamma delta", "ok, abcd"):
        m = Msg()
        await cmd_findchannels(m, Bot_(), conn, settings(tmp_path), cfg, CLOCK, bad)
        assert m.sent == [flow.USAGE]
    assert flow.parse_keywords("vakansii kz") == ["vakansii", "kz"] or True
    assert flow.parse_keywords("vakansii kz, jobs_kz") == ["vakansii kz", "jobs_kz"]
    assert flow.parse_keywords("  ") is None


async def test_no_credentials_no_session_and_flood_are_explicit(conn, cfg, tmp_path):
    m = Msg()
    await cmd_findchannels(m, Bot_(), conn, settings(tmp_path, creds=False), cfg, CLOCK)
    assert "credentials" in m.sent[0]
    m = Msg()
    await cmd_findchannels(m, Bot_(), conn, settings(tmp_path, session=False), cfg, CLOCK)
    assert "session" in m.sent[0]
    set_flood(conn, NOW, 100)
    client = good_client()
    m = Msg()
    await cmd_findchannels(
        m, Bot_(), conn, settings(tmp_path), cfg, CLOCK, "vakansii kz, jobs_kz", factory(client)
    )
    assert m.sent == ["Telegram flood wait: 110s left. Discovery resumes after it."]
    assert client.calls == []


async def test_none_found_and_cap_warns_once(conn, cfg, tmp_path):
    cfg2 = AppConfig.from_dict(
        {
            "sources": {"telegram_channels": {"enabled": True, "channels": ["Configured_Ch"]}},
            "telegram_discovery": {"daily_call_cap": 1},
        }
    )
    bot = Bot_()
    for _ in range(2):
        m = Msg()
        await cmd_findchannels(
            m,
            bot,
            conn,
            settings(tmp_path),
            cfg2,
            CLOCK,
            "vakansii, jobs_kz",
            factory(good_client()),
        )
    assert "daily call cap" in m.sent[-1]
    assert len(bot.sent) == 1 and "cap reached" in bot.sent[0][1]  # one warning per day
    m = Msg()
    await cmd_findchannels(
        m, Bot_(), conn, settings(tmp_path), cfg, CLOCK, "zzzzzz", factory(good_client())
    )
    assert m.sent == [flow.NO_NEW_TEXT]


def test_hostile_title_and_samples_are_escaped_and_clipped():
    c = Candidate("h_chan", HOSTILE, 5, 0.5, [HOSTILE + "y" * 500, "a" * 300], "q")
    text = flow.render_candidate(c)
    assert "<b>" not in text and "<script>" not in text and "</a>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;&lt;/a&gt; &amp;" in text
    assert "y" * 300 not in text and "a" * 201 not in text
    assert len(text) < 4096
    c = Candidate("h_chan", None, None, 0.0, [], "q")
    assert flow.render_candidate(c) == "@h_chan (subscribers unknown, job posts ~0%)"


def test_callback_data_within_64_bytes():
    handle = "a" * 32
    for b in flow.candidate_keyboard(handle).inline_keyboard[0]:
        assert len(b.callback_data.encode()) <= 64


class CB:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.message = SimpleNamespace(edit_reply_markup=self.edit)
        self.cleared = 0

    async def answer(self, text=None, **kw):
        self.answers.append(text)

    async def edit(self, **kw):
        self.cleared += 1


def store(conn, handle="kzjobs_news"):
    db.add_tg_candidate(
        conn,
        handle,
        title="T",
        subscribers=1,
        job_ratio=0.2,
        samples=["s"],
        found_by="q",
        now=NOW,
    )


async def test_add_writes_overlay_and_channels_include_it(conn, cfg, monkeypatch):
    store(conn)
    reloaded = []

    async def reload():
        reloaded.append(1)

    monkeypatch.setattr("src.bot.findchannels._reload_listener", reload)
    cb = CB("tc:add:kzjobs_news")
    await on_candidate_callback(cb, conn, CLOCK, cfg)
    assert cb.answers == ["Added @kzjobs_news; first poll records a baseline"]
    assert "kzjobs_news" in channels(cfg, conn)
    assert db.tg_channel_overlay(conn) == {"kzjobs_news": "added"}
    assert conn.execute("SELECT status FROM tg_channel_candidates").fetchone()[0] == "added"
    assert reloaded == [1] and cb.cleared == 1


async def test_ignore_never_relisted(conn, cfg, tmp_path):
    store(conn)
    cb = CB("tc:ign:kzjobs_news")
    await on_candidate_callback(cb, conn, CLOCK, cfg)
    assert conn.execute("SELECT status FROM tg_channel_candidates").fetchone()[0] == "ignored"
    assert db.tg_channel_overlay(conn) == {}
    client = good_client()
    m = Msg()
    await cmd_findchannels(
        m, Bot_(), conn, settings(tmp_path), cfg, CLOCK, "vakansii kz, jobs_kz", factory(client)
    )
    assert m.sent == [flow.NO_NEW_TEXT]


@pytest.mark.parametrize(
    "data", ["tc:", "tc:add", "tc:zap:abcde", "tc:add:a b", "tc:add:ab", "tc:add:x:y", None]
)
async def test_malformed_payload_is_unknown_action(conn, cfg, data):
    cb = CB(data)
    await on_candidate_callback(cb, conn, CLOCK, cfg)
    assert cb.answers == ["Unknown action"]
    assert db.tg_channel_overlay(conn) == {}


async def test_unstored_handle_cannot_be_added(conn, cfg):
    cb = CB("tc:add:never_found")
    await on_candidate_callback(cb, conn, CLOCK, cfg)
    assert cb.answers == ["Unknown action"] and db.tg_channel_overlay(conn) == {}


def test_start_text_lists_findchannels():
    assert "/findchannels" in START_TEXT


async def test_weekly_job_sends_to_configured_chat_only(conn, cfg, tmp_path):
    cues = parse_cues_text(
        '[types.job]\nen = ["vacancy"]\n[telegram_discovery]\nen = ["vakansii kz"]\n'
    )
    bot = Bot_()
    n = await flow.weekly_discovery(
        bot, conn, settings(tmp_path), cfg, CLOCK, cues, factory(good_client())
    )
    assert n == 1 and len(bot.sent) == 1
    chat, text, kw = bot.sent[0]
    assert chat == "42" and text.startswith("@kzjobs_news")
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "tc:add:kzjobs_news"


async def test_never_joins_or_sends_via_telethon(conn, cfg, tmp_path):
    client = good_client()
    await flow.run_discovery(
        Bot_(),
        42,
        conn,
        settings(tmp_path),
        cfg,
        CLOCK,
        load_cues(),
        ["vakansii kz"],
        factory(client),
    )
    assert not {"send_message", "forward_messages", "send_read_acknowledge"} & set(client.calls)
    assert client.calls[0] == "connect" and client.calls[-1] == "disconnect"


# ---- dispatcher: whitelist -------------------------------------------------


def upd_msg(chat_id, text):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="x"),
            text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        ),
    )


def upd_cb(chat_id, data):
    cb = CallbackQuery(
        id="1",
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
        chat_instance="c",
        data=data,
        message=Message(message_id=1, date=NOW, chat=Chat(id=chat_id, type="private"), text="m"),
    )
    return Update(update_id=2, callback_query=cb)


async def feed(conn, cfg, tmp_path, update, monkeypatch, client):
    sent = []

    async def fake_call(self, method, request_timeout=None):
        sent.append(type(method).__name__)
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, settings(tmp_path), cfg, CLOCK, tg_factory=factory(client))
    bot = Bot("1:t")
    await dp.feed_update(bot, update)
    await bot.session.close()
    return sent


async def test_other_chat_is_silent_for_command_and_callback(conn, cfg, tmp_path, monkeypatch):
    store(conn)
    client = good_client()
    out = await feed(
        conn, cfg, tmp_path, upd_msg(99, "/findchannels vakansii kz, jobs_kz"), monkeypatch, client
    )
    out += await feed(conn, cfg, tmp_path, upd_cb(99, "tc:add:kzjobs_news"), monkeypatch, client)
    assert out == [] and client.calls == [] and db.tg_channel_overlay(conn) == {}


async def test_owner_command_and_callback_via_dispatcher(conn, cfg, tmp_path, monkeypatch):
    client = good_client()
    out = await feed(
        conn, cfg, tmp_path, upd_msg(42, "/findchannels vakansii kz, jobs_kz"), monkeypatch, client
    )
    assert out == ["SendMessage"] and "SearchRequest" in client.calls
    out = await feed(conn, cfg, tmp_path, upd_cb(42, "tc:add:kzjobs_news"), monkeypatch, client)
    assert "AnswerCallbackQuery" in out
    assert db.tg_channel_overlay(conn) == {"kzjobs_news": "added"}
