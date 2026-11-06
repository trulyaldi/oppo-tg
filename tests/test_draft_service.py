import asyncio
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.resources.messages import AsyncMessages

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.contacts.store import RawContact, add_contact, prune_contacts
from src.cv import store
from src.llm.budget import LlmBudget
from src.models import Kind, Posting
from src.outreach import draft as dr
from src.outreach import prepare as prep

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_bait_fake.txt").read_text()
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
OK = (FX / "draft_reply_ok.json").read_text()
FAB = (FX / "draft_reply_fabricated.json").read_text()
BAIT = json.loads((FX / "posting_fabrication_bait.json").read_text())


class Fake:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


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
    clock = FixedClock(NOW)
    p = Posting(
        source="t", external_id="1", title=BAIT["title"], company=BAIT["company"],
        url="https://example.com/j", description=BAIT["description"], kind=Kind.JOB,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    store.save_cv(conn, CV, json.dumps({"skills": ["Python", "SQL"]}), "text", "m", NOW)
    cid = add_contact(
        conn, p.id, RawContact("Jane Roe", "Technical Recruiter"),
        source_kind="posting_text", now=NOW,
    )  # fmt: skip
    return conn, clock, cid


def b(conn, clock, cap=40):
    return LlmBudget(conn, clock, cap)


def n_drafts(conn):
    return conn.execute("SELECT COUNT(*) FROM outreach_drafts").fetchone()[0]


def reply(body, ask="Could you tell me how the hiring process works for this role?", quote=None):
    d = json.loads(OK)
    d["body"] = body
    d["ask"] = ask
    if quote:
        d["hook"]["quote"] = quote
    return json.dumps(d)


async def test_ok_then_cache(env):
    conn, clock, cid = env
    c = Fake(OK)
    r1 = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    r2 = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    assert isinstance(r1, dr.DraftResult) and not r1.cached and r2.cached
    assert len(c.calls) == 1 and r1.body == r2.body and n_drafts(conn) == 1
    assert r1.ask in r1.body


async def test_fabricated_then_ok(env):
    conn, clock, cid = env
    c = Fake(FAB, OK)
    r = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    assert isinstance(r, dr.DraftResult) and len(c.calls) == 2
    stored = conn.execute("SELECT body FROM outreach_drafts").fetchone()["body"]
    for bad in ("gold medal", "IMO", "Jane Street"):
        assert bad not in stored and bad not in r.body
    # correction names terms, never the CV text
    assert "Unsupported terms" in c.calls[1]["messages"][0]["content"]


async def test_fabricated_twice(env):
    conn, clock, cid = env
    c = Fake(FAB, FAB)
    r = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    assert r == dr.DraftError("unsupported") and len(c.calls) == 2 and n_drafts(conn) == 0


async def test_length_errors(env):
    conn, clock, cid = env
    two = reply(
        "I like Python and SQL for research tooling. "
        "Could you tell me how the hiring process works for this role?"
    )
    six = reply(
        "One. Two. Three. Four. Five. Could you tell me how the hiring process works for this role?"
    )  # fmt: skip
    c = Fake(two, six)
    r = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    assert r == dr.DraftError("unsupported") and n_drafts(conn) == 0


async def test_bad_hook_quote_unsupported(env):
    conn, clock, cid = env
    bad = reply(json.loads(OK)["body"], quote="a sentence that is nowhere in the posting")
    c = Fake(bad, bad)
    r = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    assert r == dr.DraftError("unsupported") and n_drafts(conn) == 0


async def test_prompt_and_kwargs(env):
    conn, clock, cid = env
    c = Fake(OK)
    await dr.draft(conn, c, cid, CFG, clock, b(conn, clock))
    kw = c.calls[0]
    assert "tools" not in kw and kw["max_tokens"] == 800
    user = kw["messages"][0]["content"]
    assert "Contact type: recruiter" in user
    assert (
        "recruiter: fit for the role, ask about the process or the right team."
        in (kw["system"][0]["text"])
    )
    assert "<cv>" in user and "<posting>" in user and "<contact>" in user


async def test_new_cv_deletes_drafts(env):
    conn, clock, cid = env
    await dr.draft(conn, Fake(OK), cid, CFG, clock, b(conn, clock))
    assert n_drafts(conn) == 1
    store.save_cv(conn, CV + "\nExtra line", json.dumps({"skills": []}), "text", "m", NOW)
    assert n_drafts(conn) == 0


async def test_forget_deletes_drafts(env):
    conn, clock, cid = env
    await dr.draft(conn, Fake(OK), cid, CFG, clock, b(conn, clock))
    store.forget_cv(conn)
    assert n_drafts(conn) == 0


async def test_prune_cascade(env):
    conn, clock, cid = env
    await dr.draft(conn, Fake(OK), cid, CFG, clock, b(conn, clock))
    late = datetime(2027, 6, 1, tzinfo=UTC)
    assert prune_contacts(conn, late) == 1
    assert n_drafts(conn) == 0


async def test_budget_exhausted(env):
    conn, clock, cid = env
    c = Fake(OK)
    r = await dr.draft(conn, c, cid, CFG, clock, b(conn, clock, cap=0))
    assert r == dr.DraftError("budget") and not c.calls


async def test_no_cv_missing_or_expired_contact(env):
    conn, clock, cid = env
    c = Fake(OK)
    assert await dr.draft(conn, c, 999, CFG, clock, b(conn, clock)) == dr.DraftError("no_contact")
    late = FixedClock(datetime(2027, 6, 1, tzinfo=UTC))
    assert await dr.draft(conn, c, cid, CFG, late, b(conn, late)) == dr.DraftError("no_contact")
    store.forget_cv(conn)
    assert await dr.draft(conn, c, cid, CFG, clock, b(conn, clock)) == dr.DraftError("no_cv")
    assert not c.calls


async def test_concurrent_one_call(env):
    conn, clock, cid = env
    c = Fake(OK)
    bg = b(conn, clock)
    await asyncio.gather(*(dr.draft(conn, c, cid, CFG, clock, bg) for _ in range(2)))
    assert len(c.calls) == 1
