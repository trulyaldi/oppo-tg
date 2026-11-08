from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.handlers import (
    NOTHING_FOUND,
    SEARCH_USAGE,
    START_TEXT,
    cmd_search,
    search_terms,
)
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cues import load_cues
from src.models import Kind, Posting, ScoreResult
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
CFG = AppConfig.load(Path(__file__).parent / "fixtures" / "config_repo_snapshot.toml")
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append((text, reply_markup, parse_mode))

    @property
    def texts(self):
        return [t for t, _, _ in self.sent]


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, n, title, company="Acme", description="", score=7.0, seen_days=1):
    p = Posting(
        source="t",
        external_id=str(n),
        title=title,
        company=company,
        url=f"https://x.example/{n}",
        kind=Kind.JOB,
        description=description,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW - timedelta(days=seen_days))
    pid = conn.execute("SELECT id FROM postings WHERE external_id=?", (str(n),)).fetchone()["id"]
    db.save_score(conn, pid, ScoreResult(score=score, reason="why"), "m", "v", NOW)
    db.set_status(conn, pid, "scored")
    return pid


async def search(conn, args):
    m = FakeMessage()
    await cmd_search(m, conn, CLOCK, CFG, args)
    return m


async def test_russian_word_finds_english_intern(conn):
    add(conn, 1, "ML Intern")
    add(conn, 2, "Accountant")
    m = await search(conn, "стажировка")
    assert len(m.sent) == 1 and "ML Intern" in m.texts[0]
    assert m.sent[0][1] is not None and m.sent[0][2] == "HTML"


async def test_company_match(conn):
    add(conn, 1, "Backend Developer", company="Kaspi.kz")
    add(conn, 2, "Backend Developer", company="Other")
    m = await search(conn, "kaspi")
    assert len(m.sent) == 1 and "Kaspi" in m.texts[0]


async def test_max_five_best_first(conn):
    for i in range(8):
        add(conn, i, f"Data Intern {i}", score=5 + i * 0.1)
    m = await search(conn, "intern")
    assert len(m.sent) == 5
    assert "Data Intern 7" in m.texts[0]


async def test_old_and_unscored_excluded(conn):
    add(conn, 1, "Old Intern", seen_days=40)
    p = Posting(source="t", external_id="9", title="Raw Intern", company="A", url="https://x/9")
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    m = await search(conn, "intern")
    assert m.texts == [NOTHING_FOUND]


@pytest.mark.parametrize("wild", ["%", "_", "100%", "a_b"])
async def test_like_wildcards_escaped(conn, wild):
    add(conn, 1, "ML Intern", description="plain text")
    m = await search(conn, wild)
    assert m.texts == [NOTHING_FOUND]


async def test_literal_percent_found(conn):
    add(conn, 1, "ML Intern", description="grow 100% yearly")
    m = await search(conn, "100%")
    assert len(m.sent) == 1


@pytest.mark.parametrize("args", [None, "", "   "])
async def test_usage(conn, args):
    assert (await search(conn, args)).texts == [SEARCH_USAGE]


def test_terms_capped_and_expanded():
    cues = load_cues()
    terms = search_terms(cues, "стажировка " + " ".join(f"w{i}" for i in range(60)))
    assert len(terms) == 30
    assert "intern" in search_terms(cues, "стажировка")


def test_start_text_lists_search():
    assert "/search" in START_TEXT


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


@pytest.mark.parametrize(("chat_id", "expect"), [(99, 0), (42, 1)])
async def test_dispatcher_owner_only(conn, monkeypatch, chat_id, expect):
    sent = []

    async def fake_call(self, method, *a, **kw):
        sent.append(getattr(method, "text", None))

    monkeypatch.setattr(Bot, "__call__", fake_call)
    dp = build_dispatcher(conn, SETTINGS, CFG, CLOCK)
    bot = Bot("1:t")
    await dp.feed_update(bot, upd(chat_id, "/search"))
    await bot.session.close()
    assert len(sent) == expect


async def test_short_tokens_match_whole_words_only(conn):
    """FIX-52: 'ai' and 'ml' do not match inside Email, HTML or Russian words (D-305)."""
    add(conn, 11, "Email marketing specialist", description="domain maintenance")
    add(conn, 12, "Бухгалтер в отдел компании", company="ТОО Ромашка")
    add(conn, 13, "Frontend (HTML, CSS)", company="Web")
    add(conn, 14, "AI engineer", company="Lab")
    add(conn, 15, "ML Intern", company="Lab")
    ai = await search(conn, "ai")
    assert len(ai.sent) == 1 and "AI engineer" in ai.texts[0]
    ml = await search(conn, "ml")
    assert len(ml.sent) == 1 and "ML Intern" in ml.texts[0]


async def test_stem_and_prefix_matching_still_work(conn):
    add(conn, 21, "ML Intern", company="Lab")
    add(conn, 22, "Стажировка аналитик", company="ТОО")
    add(conn, 23, "Accountant", company="Firm")
    m = await search(conn, "стажировка")
    assert any("ML Intern" in t for t in m.texts)
    assert any("Стажировка" in t for t in m.texts)
    assert not any("Accountant" in t for t in m.texts)
