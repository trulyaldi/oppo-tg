import inspect
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from anthropic.resources.messages import AsyncMessages
from pydantic import BaseModel

from src import db
from src.clock import FixedClock
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json


class Out(BaseModel):
    a: int


class Fake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


def budget(tmp_path, cap=10):
    conn = db.connect(tmp_path / "j.db")
    db.migrate(conn)
    return LlmBudget(conn, FixedClock(datetime(2026, 11, 1, 5, tzinfo=UTC)), cap)


async def run(client, b):
    return await call_json(
        client,
        model="m",
        system_blocks=[{"type": "text", "text": "s"}],
        user_text="u",
        schema=Out,
        max_tokens=10,
        budget=b,
        purpose="t",
    )


async def test_valid(tmp_path):
    c = Fake(['{"a": 1}'])
    r = await run(c, budget(tmp_path))
    assert r.value == Out(a=1) and r.error_kind is None and r.calls == 1
    assert set(c.calls[0]) == {"model", "max_tokens", "system", "messages"}


async def test_fenced(tmp_path):
    r = await run(Fake(['```json\n{"a": 2}\n```']), budget(tmp_path))
    assert r.value.a == 2


async def test_trailing_prose_retries(tmp_path):
    c = Fake(['{"a": 1} thanks', '{"a": 3}'])
    r = await run(c, budget(tmp_path))
    assert r.value.a == 3 and r.calls == 2
    assert c.calls[1]["messages"][1]["role"] == "assistant"
    assert all(not ({"tools", "tool_choice", "temperature"} & set(k)) for k in c.calls)


async def test_two_bad(tmp_path):
    r = await run(Fake(['{"a": "x"}', "nope"]), budget(tmp_path))
    assert r.error_kind == "invalid" and r.calls == 2 and r.value is None


async def test_api_error(tmp_path):
    r = await run(Fake([RuntimeError("boom")]), budget(tmp_path))
    assert r.error_kind == "api"


async def test_budget_exhausted(tmp_path):
    c = Fake([])
    r = await run(c, budget(tmp_path, cap=0))
    assert r.error_kind == "budget" and c.calls == [] and r.calls == 0


async def test_no_content_in_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    r = await run(Fake(["CANARY-LLM-OUT", "CANARY-LLM-OUT"]), budget(tmp_path))
    assert r.error_kind == "invalid"
    assert "CANARY-LLM-OUT" not in caplog.text
