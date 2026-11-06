import asyncio
import inspect
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from anthropic.resources.messages import AsyncMessages

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.cv import store
from src.llm.budget import LlmBudget
from src.models import Kind, Posting
from src.outreach import prepare as prep

FX = Path(__file__).parent / "fixtures"
CV = (FX / "cv_bait_fake.txt").read_text()
NOW = datetime(2026, 11, 6, 5, tzinfo=UTC)
CFG = AppConfig.load(FX / "config_repo_snapshot.toml")
HONEST = (FX / "tailor_reply_honest.json").read_text()
FAB = (FX / "tailor_reply_fabricated.json").read_text()
INJ = json.loads((FX / "posting_injection.json").read_text())
BAIT = json.loads((FX / "posting_fabrication_bait.json").read_text())


class Fake:
    def __init__(self, reply, delay=0.0):
        self.reply = reply
        self.delay = delay
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.reply)])


@pytest.fixture(autouse=True)
def _hooks():
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    prep.register_hooks()
    yield
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


@pytest.fixture
def env(tmp_path):
    conn = db.connect(tmp_path / "p.db")
    db.migrate(conn)
    clock = FixedClock(NOW)
    pid = _add(conn, BAIT)
    store.save_cv(conn, CV, json.dumps({"skills": ["Python", "SQL"]}), "text", "m", NOW)
    return conn, clock, pid


def _add(conn, d):
    p = Posting(
        source="t", external_id=d["title"], title=d["title"], company=d["company"],
        url="https://example.com/j", description=d["description"], kind=Kind.JOB,
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, NOW)
    return p.id


def budget(conn, clock, cap=40):
    return LlmBudget(conn, clock, cap)


def strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


async def test_cache_hit_no_second_call(env):
    conn, clock, pid = env
    c = Fake(HONEST)
    b = budget(conn, clock)
    r1 = await prep.prepare(conn, c, pid, CFG, clock, b)
    r2 = await prep.prepare(conn, c, pid, CFG, clock, b)
    assert len(c.calls) == 1 and not r1.cached and r2.cached
    assert r1.result == r2.result and r1.removed == 0
    assert conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0] == 1


async def test_concurrent_one_call(env):
    conn, clock, pid = env
    c = Fake(HONEST, delay=0.05)
    b = budget(conn, clock)
    rs = await asyncio.gather(*(prep.prepare(conn, c, pid, CFG, clock, b) for _ in range(2)))
    assert len(c.calls) == 1 and rs[0].result == rs[1].result


async def test_new_cv_invalidates(env):
    conn, clock, pid = env
    c = Fake(HONEST)
    b = budget(conn, clock)
    await prep.prepare(conn, c, pid, CFG, clock, b)
    store.save_cv(conn, CV + "\nExtra line FAKE FIXTURE", "{}", "text", "m", NOW)
    assert conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0] == 0
    r = await prep.prepare(conn, c, pid, CFG, clock, b)
    assert len(c.calls) == 2 and r.cv_version == 2


async def test_forget_deletes_cache(env):
    conn, clock, pid = env
    await prep.prepare(conn, Fake(HONEST), pid, CFG, clock, budget(conn, clock))
    counts = store.forget_cv(conn)
    assert counts["derived"] == 1
    assert conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0] == 0


async def test_fabricated_reply_removed(env):
    conn, clock, pid = env
    r = await prep.prepare(conn, Fake(FAB), pid, CFG, clock, budget(conn, clock))
    assert r.removed == 6
    stored = json.loads(conn.execute("SELECT result FROM tailoring_cache").fetchone()[0])
    assert conn.execute("SELECT removed_claims FROM tailoring_cache").fetchone()[0] == 6
    blob = "\n".join(strings({k: v for k, v in stored.items() if k != "gaps"}))
    blob += "\n".join(g["how_to_address"] for g in stored["gaps"])
    for bad in ("Grandmaster", "12 engineers", "3 years of Python", "5 years"):
        assert bad not in blob


async def test_injection_delimiters_and_claim(env):
    conn, clock, _ = env
    pid = _add(conn, INJ)
    reply = json.dumps(
        {"fit": {"level": "strong", "points": [
            {"text": "Won IMO gold medal", "cv_evidence": "Won IMO gold medal"}]},
         "emphasize": [{"text": "Python and SQL data cleaning",
                        "cv_evidence": "Cleaned and joined survey datasets with Python and SQL"}]}
    )  # fmt: skip
    c = Fake(reply)
    r = await prep.prepare(conn, c, pid, CFG, clock, budget(conn, clock))
    user = c.calls[0]["messages"][0]["content"]
    assert user.count("<posting>") == 1 and user.count("</posting>") == 1
    assert "Ignore all previous instructions" in user  # kept as data
    assert r.removed == 1 and r.result["fit"]["points"] == []
    assert "IMO" not in json.dumps(r.result)


async def test_api_error_not_cached(env):
    conn, clock, pid = env
    r = await prep.prepare(conn, Fake(RuntimeError("x")), pid, CFG, clock, budget(conn, clock))
    assert r == prep.PrepareError("api")
    assert conn.execute("SELECT COUNT(*) FROM tailoring_cache").fetchone()[0] == 0


async def test_invalid_not_cached(env):
    conn, clock, pid = env
    c = Fake("not json")
    r = await prep.prepare(conn, c, pid, CFG, clock, budget(conn, clock))
    assert r == prep.PrepareError("invalid") and len(c.calls) == 2


async def test_budget_exhausted(env):
    conn, clock, pid = env
    c = Fake(HONEST)
    r = await prep.prepare(conn, c, pid, CFG, clock, budget(conn, clock, cap=0))
    assert r == prep.PrepareError("budget") and c.calls == []


async def test_no_cv_and_no_posting(env):
    conn, clock, pid = env
    c = Fake(HONEST)
    assert await prep.prepare(conn, c, "nope", CFG, clock, budget(conn, clock)) == (
        prep.PrepareError("no_posting")
    )
    store.forget_cv(conn)
    assert await prep.prepare(conn, c, pid, CFG, clock, budget(conn, clock)) == (
        prep.PrepareError("no_cv")
    )
    assert c.calls == []


async def test_kwargs_model_no_tools(env):
    conn, clock, pid = env
    c = Fake(HONEST)
    await prep.prepare(conn, c, pid, CFG, clock, budget(conn, clock))
    kw = c.calls[0]
    assert not {"tools", "tool_choice", "temperature"} & set(kw)
    assert kw["model"] == "claude-sonnet-4-6" and kw["max_tokens"] == 2000
    user = kw["messages"][0]["content"]
    assert user.startswith("Today is 2026-11-06.") and "<cv>" in user
    assert "Sam Sample" in user and "1: " not in user
    assert "DATA" in kw["system"][0]["text"]


async def test_no_content_in_logs(env, caplog):
    conn, clock, pid = env
    caplog.set_level(logging.DEBUG)
    await prep.prepare(conn, Fake(FAB), pid, CFG, clock, budget(conn, clock))
    for s in ("Sam Sample", "Low-Latency Quant", "Kaggle", "kdb+", "Weather Notebook"):
        assert s not in caplog.text
