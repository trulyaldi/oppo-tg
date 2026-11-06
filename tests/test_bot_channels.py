from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.handlers import START_TEXT, cmd_channels
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.sources.telegram_channels import MAX_CHANNELS, channels

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {
        "TELEGRAM_BOT_TOKEN": "1:t",
        "TELEGRAM_CHAT_ID": "42",
        "ANTHROPIC_API_KEY": "k",
        "TG_API_ID": "1",
        "TG_API_HASH": "h",
    }
)
CLOCK = FixedClock(NOW)


class Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)


@pytest.fixture
def cfg():
    return AppConfig.from_dict(
        {"sources": {"telegram_channels": {"enabled": True, "channels": ["job_kz", "@intern_kz"]}}}
    )


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


async def run(conn, cfg, args):
    m = Msg()
    await cmd_channels(m, conn, SETTINGS, cfg, CLOCK, args)
    return m.sent


async def test_list_origin_and_status(conn, cfg):
    await run(conn, cfg, "add t.me/new_chan")
    out = await run(conn, cfg, None)
    assert len(out) == 1
    assert "@job_kz [config]" in out[0]
    assert "@new_chan [added]" in out[0]
    assert "no Telethon session" in out[0]  # no session file in the test env


async def test_add_remove(conn, cfg):
    assert "Added @new_chan" in (await run(conn, cfg, "add @new_chan"))[0]
    assert channels(cfg, conn) == ["job_kz", "intern_kz", "new_chan"]
    assert "already listed" in (await run(conn, cfg, "add NEW_CHAN"))[0]
    assert (await run(conn, cfg, "remove new_chan")) == ["Removed @new_chan."]
    assert channels(cfg, conn) == ["job_kz", "intern_kz"]
    assert "not in the list" in (await run(conn, cfg, "remove new_chan"))[0]
    await run(conn, cfg, "add new_chan")  # re-add after removal
    assert "new_chan" in channels(cfg, conn)


async def test_removing_config_channel_hides_it(conn, cfg):
    await run(conn, cfg, "remove job_kz")
    assert channels(cfg, conn) == ["intern_kz"]
    assert channels(cfg) == ["job_kz", "intern_kz"]  # config untouched
    await run(conn, cfg, "add job_kz")
    assert "job_kz" in channels(cfg, conn)


@pytest.mark.parametrize(
    "args", ["add @a", "add https://evil.com/x", "remove @a", "add a b", "bogus", "add"]
)
async def test_invalid_rejected(conn, cfg, args):
    out = await run(conn, cfg, args)
    assert len(out) == 1 and "Usage" in out[0] or "not a valid" in out[0]
    assert db.tg_channel_overlay(conn) == {}


async def test_limit_50(conn, cfg):
    for i in range(MAX_CHANNELS):
        db.set_tg_channel(conn, f"chan_{i:03d}", "added", NOW)
    assert len(channels(cfg, conn)) == MAX_CHANNELS
    out = await run(conn, cfg, "add one_more")
    assert "At most 50" in out[0]


async def test_added_channel_is_new_target_baseline(conn, cfg):
    from types import SimpleNamespace

    from src.sources.telegram_channels import TelegramChannelsSource

    db.mark_baselined(conn, "telegram_channels", "job_kz", NOW)
    await run(conn, cfg, "add new_chan")
    ctx = SimpleNamespace(settings=SETTINGS, cfg=cfg, http=SimpleNamespace(db=conn))
    src = TelegramChannelsSource(require_session_file=False)
    keys = [t.key for t in src.targets(ctx)]
    assert "new_chan" in keys
    assert db.get_target_state(conn, "telegram_channels", "new_chan") is None


def test_start_text_lists_channels():
    assert "/channels" in START_TEXT


def upd(chat_id, text):
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


async def dispatch(conn, cfg, chat_id, text, monkeypatch):
    sent = []

    async def fake_call(self, method, *a, **kw):
        sent.append(getattr(method, "text", None))

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, SETTINGS, cfg, CLOCK)
    bot = Bot("1:t")
    await dp.feed_update(bot, upd(chat_id, text))
    await bot.session.close()
    return sent


async def test_other_chat_ignored(conn, cfg, monkeypatch):
    assert await dispatch(conn, cfg, 99, "/channels add new_chan", monkeypatch) == []
    assert db.tg_channel_overlay(conn) == {}


async def test_owner_via_dispatcher(conn, cfg, monkeypatch):
    out = await dispatch(conn, cfg, 42, "/channels add new_chan", monkeypatch)
    assert out == ["Added @new_chan. Its first poll is a baseline."]
    assert db.tg_channel_overlay(conn) == {"new_chan": "added"}
