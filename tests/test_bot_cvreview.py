import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.resources.messages import AsyncMessages

from src import db
from src.bot.handlers import START_TEXT
from src.bot.outreach_handlers import cmd_cvreview, on_text
from src.clock import FixedClock
from src.config import AppConfig
from src.cv import review as rv
from src.cv import store

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_fake.txt").read_text()
PARSE = (FX / "cv_parse_reply.json").read_text()
OK = (FX / "cv_review_reply_ok.json").read_text()
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 8, 5, tzinfo=UTC)


class Fake:
    """Replies in order: first call parses the CV, later calls review it."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []
        self.docs = []

    async def answer(self, text, parse_mode=None, **kw):
        self.sent.append(text)

    async def answer_document(self, document, **kw):
        self.docs.append(document)


@pytest.fixture(autouse=True)
def _hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    rv.register_hooks()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "d.db")
    db.migrate(c)
    return c


def files(root):
    return sorted(str(p) for p in root.rglob("*"))


async def test_upload_sends_summary_and_document(conn, tmp_path):
    clock = FixedClock(NOW)
    client = Fake(PARSE, OK)
    before = files(tmp_path)
    m = Msg(CV)
    await on_text(m, conn, clock, CFG, client)
    assert "CV v1 saved" in m.sent[0]
    summary = m.sent[1]
    assert len(m.sent) == 2 and len(summary) <= 3500
    assert "add if true" in summary and "worth learning" in summary
    assert "1. " in summary and "5. " in summary and "6. " not in summary
    assert len(m.docs) == 1
    doc = m.docs[0]
    cached = conn.execute("SELECT report_md FROM cv_reviews").fetchone()[0]
    assert doc.data == cached.encode() and doc.filename == "cv_review_v1.md"
    assert files(tmp_path) == before
    assert len(client.calls) == 2


async def test_identical_cv_no_review(conn):
    clock = FixedClock(NOW)
    client = Fake(PARSE, OK)
    await on_text(Msg(CV), conn, clock, CFG, client)
    m = Msg(CV)
    await on_text(m, conn, clock, CFG, client)
    assert "unchanged" in m.sent[0] and not m.docs
    assert len(client.calls) == 2


async def test_cvreview_cached_then_refresh(conn):
    clock = FixedClock(NOW)
    client = Fake(PARSE, OK)
    await on_text(Msg(CV), conn, clock, CFG, client)
    m = Msg()
    await cmd_cvreview(m, conn, clock, CFG, client, None)
    assert len(client.calls) == 2 and len(m.docs) == 1 and "CV review (v1)" in m.sent[0]
    m2 = Msg()
    await cmd_cvreview(m2, conn, clock, CFG, client, "refresh")
    assert len(client.calls) == 3 and len(m2.docs) == 1
    m3 = Msg()
    await cmd_cvreview(m3, conn, clock, CFG, client, "bogus")
    assert "Usage" in m3.sent[0] and len(client.calls) == 3


async def test_error_states(conn):
    clock = FixedClock(NOW)
    m = Msg()
    await cmd_cvreview(m, conn, clock, CFG, Fake(OK), None)
    assert m.sent == ["Upload your CV first with /cv."] and not m.docs
    store.save_cv(conn, CV, json.dumps({"skills": ["Python"]}), "text", "m", NOW)
    m2 = Msg()
    await cmd_cvreview(m2, conn, clock, CFG, None, None)
    assert "Could not review" in m2.sent[0]
    m3 = Msg()
    await cmd_cvreview(m3, conn, clock, CFG, Fake("not json"), None)
    assert "Could not review" in m3.sent[0] and not m3.docs


async def test_budget_reply(conn):
    clock = FixedClock(NOW)
    store.save_cv(conn, CV, json.dumps({"skills": ["Python"]}), "text", "m", NOW)
    cfg = AppConfig.from_dict({"outreach": {"daily_llm_call_cap": 0}})
    client = Fake(OK)
    m = Msg()
    await cmd_cvreview(m, conn, clock, cfg, client, None)
    assert "limit reached" in m.sent[0] and not client.calls


def test_start_text_lists_cvreview():
    assert "/cvreview" in START_TEXT
