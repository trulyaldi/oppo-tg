"""FIX-54: every newer surface is silent for another chat; the send call sites are pinned."""

import ast
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Document, Message, Update, User
from test_bot_findchannels import (  # noqa: F401
    Bot_,
    cfg,
    conn,  # noqa: F401
    good_client,
    settings,
)
from test_bot_findchannels import factory as tg_factory
from test_bot_search import CFG, CLOCK, NOW, SETTINGS

from src import db
from src.bot.app import build_dispatcher
from src.cues import parse_cues_text
from src.discovery import telegram_flow as flow

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("postings", "tg_channels", "cv_reviews", "tg_channel_candidates")


def _msg(chat_id, text=None, **kw):
    return Message(
        message_id=1,
        date=NOW,
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
        text=text,
        **kw,
    )


def _cmd(chat_id, text):
    ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update(update_id=1, message=_msg(chat_id, text, entities=ents))


def _forward(chat_id):
    origin = {
        "type": "channel",
        "date": NOW,
        "chat": Chat(id=-100123, type="channel", username="KazJobs"),
        "message_id": 7,
    }
    return Update(
        update_id=1,
        message=_msg(chat_id, "Intern at Acme https://x.example/9", forward_origin=origin),
    )


def _doc(chat_id):
    doc = Document(file_id="f", file_unique_id="u", file_name="cv.pdf", mime_type="application/pdf")
    return Update(update_id=1, message=_msg(chat_id, document=doc))


def _edited(chat_id):
    return Update(update_id=1, edited_message=_msg(chat_id, "/add Intern https://x.example/1"))


def _cb(chat_id, data):
    cb = CallbackQuery(
        id="1",
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
        chat_instance="c",
        data=data,
        message=_msg(chat_id, "m"),
    )
    return Update(update_id=1, callback_query=cb)


SURFACES = {
    "add_text": lambda c: _cmd(c, "/add Intern at Acme https://x.example/1"),
    "forward": _forward,
    "search": lambda c: _cmd(c, "/search x"),
    "findchannels": lambda c: _cmd(c, "/findchannels"),
    "tc_add": lambda c: _cb(c, "tc:add:somechannel"),
    "tc_ign": lambda c: _cb(c, "tc:ign:somechannel"),
    "cvreview": lambda c: _cmd(c, "/cvreview"),
    "cvreview_refresh": lambda c: _cmd(c, "/cvreview refresh"),
    "boards": lambda c: _cmd(c, "/boards"),
    "channels": lambda c: _cmd(c, "/channels"),
    "stats": lambda c: _cmd(c, "/stats"),
    "document": _doc,
    "edited": _edited,
}


def _counts(conn):  # noqa: F811
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


@pytest.mark.parametrize("name", list(SURFACES))
async def test_other_chat_gets_zero_bot_calls(conn, monkeypatch, name):  # noqa: F811
    calls = []

    async def fake_call(self, method, *a, **kw):
        calls.append(type(method).__name__)

    monkeypatch.setattr(Bot, "__call__", fake_call)
    before = _counts(conn)
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK)
    bot = Bot("1:t")
    await dp.feed_update(bot, SURFACES[name](99))
    await bot.session.close()
    assert calls == []
    assert _counts(conn) == before


CUES = '[types.job]\nen = ["vacancy"]\n[telegram_discovery]\nen = ["vakansii kz"]\n'


async def test_weekly_discovery_targets_configured_chat(cfg, tmp_path):  # noqa: F811
    c = db.connect(":memory:")
    db.migrate(c)
    bot = Bot_()
    await flow.weekly_discovery(
        bot, c, settings(tmp_path), cfg, CLOCK, parse_cues_text(CUES), tg_factory(good_client())
    )
    assert bot.sent and all(str(chat) == "42" for chat, _t, _kw in bot.sent)


async def test_cap_warning_targets_configured_chat(cfg, tmp_path):  # noqa: F811
    # A zero cap makes the first reserve fail, so the once-per-day cap warning is sent.
    capped = type(cfg).from_dict(
        {
            "sources": {"telegram_channels": {"enabled": True, "channels": ["Configured_Ch"]}},
            "telegram_discovery": {"daily_call_cap": 0},
        }
    )
    c = db.connect(":memory:")
    db.migrate(c)
    bot = Bot_()
    await flow.weekly_discovery(
        bot, c, settings(tmp_path), capped, CLOCK, parse_cues_text(CUES), tg_factory(good_client())
    )
    assert bot.sent and all(str(chat) == "42" for chat, _t, _kw in bot.sent)
    assert any("cap" in t.lower() for _c, t, _k in bot.sent)


# ---- send call sites -------------------------------------------------------

SEND_METHODS = {"send_message", "send_document", "answer_document"}
# file -> (method, first-argument source). A new site fails until reviewed and added here.
KNOWN_SITES = {
    "src/delivery/digest.py": [
        ("send_message", "settings.telegram_chat_id"),
        ("send_message", "chat_id"),
        ("send_message", "chat_id"),
        ("send_message", "chat_id"),
    ],
    "src/delivery/alerts.py": [("send_message", "chat_id")],
    "src/pipeline/scoring_alert.py": [("send_message", "chat_id"), ("send_message", "chat_id")],
    "src/discovery/telegram_budget.py": [("send_message", "chat_id")],
    "src/discovery/telegram_flow.py": [("send_message", "chat_id")],
    "src/sources/hh.py": [("send_message", "chat_id")],
    "src/main.py": [("send_message", "self.chat_id"), ("send_document", "self.chat_id")],
    # A reply to the whitelisted incoming message (no explicit chat argument).
    "src/bot/outreach_handlers.py": [("answer_document", "<reply>")],
}


def _sites():
    found: dict[str, list[tuple[str, str]]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in SEND_METHODS:
                continue
            first = "<reply>" if node.func.attr == "answer_document" else ast.unparse(node.args[0])
            found.setdefault(rel, []).append((node.func.attr, first))
    return found


def test_send_call_sites_are_the_reviewed_set():
    found = _sites()
    assert {k: sorted(v) for k, v in found.items()} == {
        k: sorted(v) for k, v in KNOWN_SITES.items()
    }


def test_every_send_site_targets_the_configured_chat():
    ok = {"chat_id", "self.chat_id", "settings.telegram_chat_id", "<reply>"}
    for rel, sites in _sites().items():
        for method, target in sites:
            assert target in ok, f"{rel}: {method}({target}) is not the configured chat"
