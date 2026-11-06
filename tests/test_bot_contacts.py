import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.outreach_handlers import (
    URL_ONLY,
    cmd_addcontact,
    cmd_contacts,
    on_add_contact_callback,
    on_contact_text,
)
from src.bot.pending import peek_pending, set_pending
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.models import Kind, Posting
from tests.test_contacts_pasted import GOOD, PROFILE, Fake

FX = Path(__file__).parent / "fixtures"
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CLOCK = FixedClock(NOW)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)
LI = "https://www.linkedin.com/in/fake-person"
SHORT = f"{LI}\nAlex Placeholder - Senior Recruiter at Acme Fake"


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []
        self.markups = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)
        self.markups.append(kw.get("reply_markup"))


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def pid(conn):
    p = Posting(
        source="t", external_id="1", title="Quant Researcher", company="Acme Fake",
        url="https://acme-fake.test/j", description="d", kind=Kind.JOB,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    return p.id


def rows(conn):
    return conn.execute("SELECT * FROM contacts").fetchall()


async def test_short_paste_stores_without_http_or_llm(conn, pid):
    seen = []
    http = build_client(
        conn, CLOCK, transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
        min_interval_s=0,
    )  # fmt: skip
    client = Fake(GOOD)
    set_pending(conn, "contact", pid, NOW, 15)
    m = Msg(SHORT)
    await on_contact_text(m, conn, CLOCK, CFG, client)
    await http.aclose()
    r = rows(conn)[0]
    assert (
        r["source_url"] == LI and r["contact_type"] == "recruiter" and r["source_kind"] == "pasted"
    )
    assert r["company"] == "Acme Fake"
    assert m.sent == [f"Saved recruiter contact #{r['id']}. /draft_{r['id']}"]
    assert not seen and not client.calls
    assert peek_pending(conn, NOW) is None


async def test_url_only_keeps_pending(conn, pid):
    set_pending(conn, "contact", pid, NOW, 15)
    m = Msg(LI)
    await on_contact_text(m, conn, CLOCK, CFG, Fake(GOOD))
    assert m.sent == [URL_ONLY] and not rows(conn)
    assert peek_pending(conn, NOW) is not None


async def test_long_profile_one_llm_call_type_from_classifier(conn, pid):
    client = Fake({**GOOD, "type": "founder"})
    set_pending(conn, "contact", pid, NOW, 15)
    m = Msg(PROFILE)
    await on_contact_text(m, conn, CLOCK, CFG, client)
    r = rows(conn)[0]
    assert len(client.calls) == 1
    assert r["contact_type"] == "engineer" and r["company"] == "Acme Fake Capital"
    assert "@" not in r["note"] and "555" not in r["note"]
    assert m.sent[0].startswith("Saved engineer contact #")


async def test_long_profile_ungrounded_name_not_stored(conn, pid):
    set_pending(conn, "contact", pid, NOW, 15)
    m = Msg(PROFILE)
    await on_contact_text(m, conn, CLOCK, CFG, Fake({**GOOD, "name": "Mallory Nowhere"}))
    assert not rows(conn) and "not saved" in m.sent[0]


async def test_expired_pending_ignored(conn, pid):
    set_pending(conn, "contact", pid, NOW - timedelta(minutes=30), 15)
    m = Msg(SHORT)
    client = Fake(GOOD)
    await on_contact_text(m, conn, CLOCK, CFG, client)
    assert not rows(conn) and not m.sent and not client.calls


async def test_addcontact_with_text_and_without(conn, pid):
    m = Msg()
    await cmd_addcontact(m, conn, CLOCK, CFG, None, f"{pid[:8]} Sam Example - Head of Research")
    assert rows(conn)[0]["contact_type"] == "hiring_manager"
    m2 = Msg()
    await cmd_addcontact(m2, conn, CLOCK, CFG, None, pid)
    p = peek_pending(conn, NOW)
    assert p.kind == "contact" and p.posting_id == pid and "never open it" in m2.sent[0]
    m3 = Msg()
    await cmd_addcontact(m3, conn, CLOCK, CFG, None, None)
    assert m3.sent[0].startswith("Usage")
    m4 = Msg()
    await cmd_addcontact(m4, conn, CLOCK, CFG, None, "zzzzzzzz x")
    assert m4.sent == ["Posting not found."]


async def test_duplicate_not_saved_twice(conn, pid):
    for _ in range(2):
        m = Msg()
        await cmd_addcontact(m, conn, CLOCK, CFG, None, f"{pid} {SHORT}")
    assert len(rows(conn)) == 1 and m.sent[0].startswith("Not saved")


async def test_contacts_list_with_linkedin_buttons(conn, pid):
    await cmd_addcontact(Msg(), conn, CLOCK, CFG, None, f"{pid} {SHORT}")
    m = Msg()
    await cmd_contacts(m, conn, CLOCK, pid)
    text = "\n".join(m.sent)
    assert "Alex Placeholder" in text and "recruiter (90%)" in text and LI in text
    kb = m.markups[-1]
    urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]
    assert len(urls) == 2 and all("linkedin.com/search/results/people" in u for u in urls)
    assert datas == [f"ac:{pid}"]
    m2 = Msg()
    await cmd_contacts(m2, conn, CLOCK, None)
    assert m2.sent[0].startswith("Usage")


async def test_callback_answers_then_sets_pending(conn, pid):
    events = []
    m = Msg()

    class Cb:
        data = f"ac:{pid}"
        message = m

        async def answer(self, text=None, **kw):
            events.append("answer")

    await on_add_contact_callback(Cb(), conn, CLOCK, CFG)
    assert events == ["answer"] and peek_pending(conn, NOW).kind == "contact"


async def test_no_pasted_text_in_logs(conn, pid, caplog):
    caplog.set_level(logging.DEBUG)
    set_pending(conn, "contact", pid, NOW, 15)
    await on_contact_text(Msg(PROFILE), conn, CLOCK, CFG, Fake("not json"))
    await cmd_addcontact(Msg(), conn, CLOCK, CFG, None, f"{pid} {SHORT}")
    assert "Samplesmith" not in caplog.text and "Placeholder" not in caplog.text
    assert "microstructure" not in caplog.text


# --- dispatcher -----------------------------------------------------------------


def upd(chat_id, text=None, data=None, uid=1):
    user = User(id=chat_id, is_bot=False, first_name="x")
    chat = Chat(id=chat_id, type="private")
    if data is not None:
        cb = CallbackQuery(
            id="1", from_user=user, chat_instance="c", data=data,
            message=Message(message_id=1, date=NOW, chat=chat, text="m"),
        )  # fmt: skip
        return Update(update_id=uid, callback_query=cb)
    ents = None
    if text.startswith("/"):
        ents = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update(
        update_id=uid,
        message=Message(
            message_id=1, date=NOW, chat=chat, from_user=user, text=text, entities=ents
        ),
    )


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


async def test_dispatcher_flow_and_whitelist(conn, pid, monkeypatch):
    client = Fake(GOOD)
    # other chats are dropped, for button, command and text alike
    assert await feed(conn, client, upd(99, data=f"ac:{pid}"), monkeypatch) == []
    assert peek_pending(conn, NOW) is None
    assert await feed(conn, client, upd(99, f"/addcontact {pid} {SHORT}"), monkeypatch) == []
    assert not rows(conn)
    # button -> pending -> plain text is consumed
    calls = await feed(conn, client, upd(42, data=f"ac:{pid}"), monkeypatch)
    assert type(calls[0]).__name__ == "AnswerCallbackQuery"
    calls = await feed(conn, client, upd(42, SHORT), monkeypatch)
    assert texts(calls)[-1].startswith("Saved recruiter contact #") and not client.calls
    # no pending: plain text is ignored
    assert await feed(conn, client, upd(42, SHORT), monkeypatch) == []
    calls = await feed(conn, client, upd(42, f"/contacts_{pid}"), monkeypatch)
    assert "Alex Placeholder" in texts(calls)[-1]
    calls = await feed(conn, client, upd(42, f"/contacts {pid[:8]}"), monkeypatch)
    assert "Alex Placeholder" in texts(calls)[-1]
    calls = await feed(conn, client, upd(42, f"/addcontact {pid}"), monkeypatch)
    assert peek_pending(conn, NOW).kind == "contact"
    calls = await feed(conn, client, upd(42, PROFILE), monkeypatch)
    assert len(client.calls) == 1 and "Saved engineer contact" in texts(calls)[-1]
