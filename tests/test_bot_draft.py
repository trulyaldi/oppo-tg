import json
import logging
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.outreach_handlers import cmd_draft, on_draft_callback
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.contacts.store import RawContact, add_contact
from src.cv import store
from src.models import Kind, Posting
from src.outreach import draft as dr
from src.outreach import prepare as prep
from src.outreach.render import prepare_keyboard, render_draft
from tests.test_draft_service import Fake

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_bait_fake.txt").read_text()
OK = (FX / "draft_reply_ok.json").read_text()
BAIT = json.loads((FX / "posting_fabrication_bait.json").read_text())
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
ROOT = Path(__file__).parent.parent


class Msg:
    def __init__(self):
        self.sent = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)


class CB:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.events = []

    async def answer(self, text=None, **kw):
        self.events.append("answered")


@pytest.fixture(autouse=True)
def _hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    prep.register_hooks()
    dr.register_hooks()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    db.migrate(conn)
    p = Posting(
        source="t", external_id="1", title=BAIT["title"], company=BAIT["company"],
        url="https://example.com/j", description=BAIT["description"], kind=Kind.JOB,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    store.save_cv(conn, CV, json.dumps({"skills": ["Python", "SQL"]}), "text", "m", NOW)
    cid = add_contact(
        conn, p.id, RawContact("Jane <Roe> & Co", "Technical Recruiter"),
        source_kind="posting_text", now=NOW,
    )  # fmt: skip
    return conn, cid


async def test_draft_command_shows_escaped_pre(env):
    conn, cid = env
    c, m = Fake(OK), Msg()
    await cmd_draft(m, conn, CLOCK, CFG, c, str(cid))
    assert len(m.sent) == 1 and len(c.calls) == 1
    out = m.sent[0]
    head, rest = out.split("\n", 1)
    assert head == "Draft for Jane &lt;Roe&gt; &amp; Co (recruiter). Not sent: copy it yourself."
    n = conn.execute("SELECT removed_claims FROM outreach_drafts").fetchone()["removed_claims"]
    assert n == 0  # the ok fixture is honest (FIX-34 fixed a false removal), so no note
    note = ""
    assert rest.startswith("<pre>") and rest.endswith("</pre>" + note) and len(out) <= 4000
    body = conn.execute("SELECT body FROM outreach_drafts").fetchone()["body"]
    assert rest == "<pre>" + body.replace("&", "&amp;").replace("<", "&lt;") + "</pre>" + note


def test_render_draft_escapes_and_clips():
    out = render_draft("A", "other", "<b>&" * 3000)
    assert len(out) <= 4000 and "<b>" not in out and out.endswith("</pre>")
    assert "&lt;b&gt;&amp;" in out


def test_render_draft_removed_note():
    base = render_draft("A", "other", "body text")
    assert render_draft("A", "other", "body text", 0) == base
    out = render_draft("A", "other", "body text", 1)
    assert out == base + "\nRemoved 1 unsupported statements."
    big = render_draft("A", "other", "x" * 9000, 2)
    assert len(big) <= 4000 and big.endswith("</pre>\nRemoved 2 unsupported statements.")


async def test_draft_command_reports_removed(env, monkeypatch):
    conn, cid = env

    async def fake_make(*a, **k):
        return dr.DraftResult(cid, "p", 1, "One. Two. Three.", "h", "a", 1, False)

    monkeypatch.setattr("src.bot.outreach_handlers.make_draft", fake_make)
    m = Msg()
    await cmd_draft(m, conn, CLOCK, CFG, Fake(OK), str(cid))
    assert len(m.sent) == 1 and len(m.sent[0]) <= 4000
    head, rest = m.sent[0].split("\n", 1)
    assert rest == "<pre>One. Two. Three.</pre>\nRemoved 1 unsupported statements."


async def test_unknown_and_bad_ids(env):
    conn, _ = env
    c, m = Fake(OK), Msg()
    await cmd_draft(m, conn, CLOCK, CFG, c, "99999")
    await cmd_draft(m, conn, CLOCK, CFG, c, "abc")
    await cmd_draft(m, conn, CLOCK, CFG, c, None)
    assert m.sent[0] == "Contact not found or expired."
    assert m.sent[1].startswith("Usage") and m.sent[2].startswith("Usage") and not c.calls


async def test_no_cv_budget_and_unsupported(env):
    conn, cid = env
    FAB = (FX / "draft_reply_fabricated.json").read_text()
    m = Msg()
    await cmd_draft(m, conn, CLOCK, CFG, Fake(FAB, FAB), str(cid))
    assert m.sent == ["Could not write a draft without unsupported claims."]
    conn.execute("DELETE FROM cv_versions")
    m = Msg()
    await cmd_draft(m, conn, CLOCK, CFG, Fake(OK), str(cid))
    assert m.sent == ["Upload your CV first with /cv."]


async def test_callback_answers_first(env):
    conn, cid = env
    m = Msg()
    cb = CB(f"dr:{cid}", m)
    await on_draft_callback(cb, conn, CLOCK, CFG, Fake(OK))
    assert cb.events == ["answered"] and len(m.sent) == 1 and m.sent[0].startswith("Draft for")


def test_callback_data_fits():
    kb = prepare_keyboard("p" * 16, [{"id": 10**12, "contact_type": "other"}], [], False)
    data = kb.inline_keyboard[0][0].callback_data
    assert data == f"dr:{10**12}" and len(data.encode()) <= 64


def upd(chat_id, text=None, data=None, uid=1):
    user = User(id=chat_id, is_bot=False, first_name="x")
    chat = Chat(id=chat_id, type="private")
    if data is not None:
        cb = CallbackQuery(
            id="1", from_user=user, chat_instance="c", data=data,
            message=Message(message_id=1, date=NOW, chat=chat, text="m"),
        )  # fmt: skip
        return Update(update_id=uid, callback_query=cb)
    ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update(
        update_id=uid,
        message=Message(
            message_id=1, date=NOW, chat=chat, from_user=user, text=text, entities=ents
        ),
    )  # fmt: skip


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


def sends(calls):
    return [c for c in calls if type(c).__name__ == "SendMessage"]


async def test_dispatcher_whitelist_and_single_send(env, monkeypatch):
    conn, cid = env
    c = Fake(OK)
    assert await feed(conn, c, upd(99, f"/draft {cid}"), monkeypatch) == []
    assert await feed(conn, c, upd(99, data=f"dr:{cid}"), monkeypatch) == []
    assert not c.calls
    calls = await feed(conn, c, upd(42, f"/draft_{cid}"), monkeypatch)
    s = sends(calls)
    assert len(s) == 1 and str(s[0].chat_id) == "42" and s[0].text.startswith("Draft for")
    calls = await feed(conn, c, upd(42, data=f"dr:{cid}"), monkeypatch)
    assert type(calls[0]).__name__ == "AnswerCallbackQuery"
    s = sends(calls)
    assert len(s) == 1 and str(s[0].chat_id) == "42" and len(c.calls) == 1  # cached
    calls = await feed(conn, c, upd(42, "/draft 99999"), monkeypatch)
    assert sends(calls)[0].text == "Contact not found or expired."


def test_hooks_registered_by_handlers_import():
    code = (
        "import src.bot.app\n"
        "from src.cv import store\n"
        "names=[f.__module__ for f in store.on_new_cv_version+store.on_forget_cv]\n"
        "assert names.count('src.outreach.draft')==2, names\n"
        "assert names.count('src.outreach.prepare')==2, names\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


async def test_cv_change_and_forget_clear_drafts_via_handlers(env):
    conn, cid = env
    await cmd_draft(Msg(), conn, CLOCK, CFG, Fake(OK), str(cid))
    n = "SELECT COUNT(*) FROM outreach_drafts"
    assert conn.execute(n).fetchone()[0] == 1
    from src.bot.outreach_handlers import cmd_forgetcv

    store.on_forget_cv.clear()
    await cmd_forgetcv(Msg(), conn, CLOCK)
    assert conn.execute(n).fetchone()[0] == 0


async def test_no_draft_text_in_logs(env, caplog):
    conn, cid = env
    caplog.set_level(logging.DEBUG)
    m = Msg()
    await cmd_draft(m, conn, CLOCK, CFG, Fake(OK), str(cid))
    body = conn.execute("SELECT body FROM outreach_drafts").fetchone()["body"]
    assert body[:40] not in caplog.text


def test_no_send_paths_in_outreach_code():
    files = [
        *(ROOT / "src/outreach").glob("*.py"),
        *(ROOT / "src/contacts").rglob("*.py"),
        ROOT / "src/bot/outreach_handlers.py",
    ]
    bad = re.compile(r"smtplib|send_mail|aiosmtplib|telethon|send_message|\.send_\w+\(")
    for f in files:
        assert not bad.search(f.read_text()), f
    # replies go through message.answer only (the whitelisted chat)
    assert "send_message" not in (ROOT / "src/bot/outreach_handlers.py").read_text()
