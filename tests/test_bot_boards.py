from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.handlers import BOARDS_USAGE, NO_BOARDS, START_TEXT, cmd_boards
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.discovery import store

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)


class Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)


def make_cfg(tmp_path):
    return AppConfig.from_dict(
        {
            "sources": {"greenhouse": {"enabled": True, "boards": ["imc"]}},
            "discovery": {"boards_file": str(tmp_path / "b.toml")},
        }
    )


@pytest.fixture
def cfg(tmp_path):
    c = make_cfg(tmp_path)
    store.add(c.discovery.boards_file, "greenhouse", "acme", "Acme Inc", NOW)
    store.add(c.discovery.boards_file, "lever", "Lev", "<b>Evil</b> & Co", NOW)
    return c


async def run(cfg, args):
    m = Msg()
    await cmd_boards(m, cfg, args)
    return m.sent


async def test_list_rendering_pinned(cfg):
    out = await run(cfg, None)
    assert out == [
        "Discovered boards (2):\n"
        "1. greenhouse:acme - Acme Inc (2026-11-07, discovered)\n"
        "2. lever:Lev - &lt;b&gt;Evil&lt;/b&gt; &amp; Co (2026-11-07, discovered)"
    ]


async def test_empty(tmp_path):
    assert await run(make_cfg(tmp_path), "") == [NO_BOARDS]


async def test_remove_by_index_and_key(cfg):
    path = cfg.discovery.boards_file
    out = await run(cfg, "remove 1")
    assert out == ["Removed greenhouse:acme."]
    assert store.active_boards(path, "greenhouse") == []
    out = await run(cfg, "remove LEVER:lev")
    assert out == ["Removed lever:Lev."]
    assert store.active_boards(path, "lever") == []
    assert store.is_known(path, "lever", "lev")  # tombstoned, not re-discoverable
    assert await run(cfg, None) == [NO_BOARDS]


async def test_removed_gone_from_targets(cfg):
    from types import SimpleNamespace

    from src.sources.base import discovered_targets

    ctx = SimpleNamespace(cfg=cfg)
    assert [t.key for t in discovered_targets(ctx, "greenhouse", ["imc"])] == ["acme"]
    await run(cfg, "remove greenhouse:acme")
    assert discovered_targets(ctx, "greenhouse", ["imc"]) == []


@pytest.mark.parametrize(
    "args",
    [
        "remove 0",
        "remove 3",
        "remove -1",
        "remove x",
        "remove nope:x",
        "remove",
        "foo",
        "remove 1 2",
    ],
)
async def test_bad_args_usage(cfg, args):
    assert await run(cfg, args) == [BOARDS_USAGE]
    assert len(store.active_boards(cfg.discovery.boards_file, "greenhouse")) == 1


async def test_chunked(tmp_path):
    c = make_cfg(tmp_path)
    for i in range(120):
        store.add(c.discovery.boards_file, "greenhouse", f"tok{i}", "C" * 100, NOW)
    out = await run(c, None)
    assert len(out) > 1 and all(len(x) <= 4096 for x in out)


def test_start_text_lists_boards():
    assert "/boards" in START_TEXT


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


async def dispatch(cfg, chat_id, text, monkeypatch):
    sent = []

    async def fake_call(self, method, *a, **kw):
        sent.append(getattr(method, "text", None))

    monkeypatch.setattr(Bot, "__call__", fake_call)
    conn = db.connect(":memory:")
    db.migrate(conn)
    dp = build_dispatcher(conn, SETTINGS, cfg, FixedClock(NOW))
    bot = Bot("1:t")
    await dp.feed_update(bot, upd(chat_id, text))
    await bot.session.close()
    return sent


async def test_other_chat_ignored(cfg, monkeypatch):
    assert await dispatch(cfg, 99, "/boards remove 1", monkeypatch) == []
    assert len(store.active_boards(cfg.discovery.boards_file, "greenhouse")) == 1


async def test_owner_via_dispatcher(cfg, monkeypatch):
    out = await dispatch(cfg, 42, "/boards remove 1", monkeypatch)
    assert out == ["Removed greenhouse:acme."]
