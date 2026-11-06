import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src import db
from src.clock import FixedClock
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint
from src.pipeline.run import score_pending
from src.scoring.scorer import Scorer

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
GOOD = json.dumps(
    {"score": 8.5, "reason": "Great fit", "deadline": "2026-12-01", "red_flags": [], "tags": ["ml"]}
)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if self.replies else GOOD
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


def add(conn, n):
    ids = []
    for i in range(n):
        p = Posting(
            source="t",
            external_id=str(i),
            title=f"Quant intern {i}",
            company="Acme",
            url="https://example.com/x",
            kind=Kind.INTERNSHIP,
        )
        db.upsert_posting(conn, p, fingerprint(p), NOW)
        ids.append(p.id)
    return ids


def scorer(conn, client, max_calls=100):
    return Scorer(client, "test-model", conn, FixedClock(NOW), max_calls)


def status(conn, pid):
    return conn.execute("SELECT status, score_attempts FROM postings WHERE id=?", (pid,)).fetchone()


async def test_valid_saved(conn):
    (pid,) = add(conn, 1)
    client = FakeClient([GOOD])
    await scorer(conn, client).score_pending(10)
    row = conn.execute("SELECT * FROM scores WHERE posting_id=?", (pid,)).fetchone()
    assert row["score"] == 8.5 and row["model"] == "test-model" and row["prompt_version"]
    assert status(conn, pid)["status"] == "scored"
    kw = client.calls[0]
    assert kw["temperature"] == 0 and kw["max_tokens"] == 300 and kw["model"] == "test-model"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_retry_with_correction(conn):
    (pid,) = add(conn, 1)
    client = FakeClient(["not json at all", GOOD])
    await scorer(conn, client).score_pending(10)
    assert len(client.calls) == 2
    msgs = client.calls[1]["messages"]
    assert msgs[1] == {"role": "assistant", "content": "not json at all"}
    assert "only the JSON" in msgs[2]["content"]
    assert status(conn, pid)["status"] == "scored"


async def test_invalid_twice(conn):
    (pid,) = add(conn, 1)
    client = FakeClient(["bad", "worse"])
    await scorer(conn, client).score_pending(10)
    assert len(client.calls) == 2
    s = status(conn, pid)
    assert s["status"] == "score_error" and s["score_attempts"] == 1


async def test_client_exception_continues(conn):
    ids = add(conn, 2)
    client = FakeClient([RuntimeError("boom"), GOOD])
    stats = await scorer(conn, client).score_pending(10)
    assert stats["errors"] == 1 and stats["scored"] == 1
    assert sorted(status(conn, i)["status"] for i in ids) == ["score_error", "scored"]


async def test_max_calls_cap(conn):
    add(conn, 5)
    client = FakeClient([])
    stats = await score_pending(conn, client, "m", FixedClock(NOW), max_calls=2)
    assert len(client.calls) == 2
    assert stats["scored"] == 2
    left = conn.execute("SELECT COUNT(*) FROM postings WHERE status='new'").fetchone()[0]
    assert left == 3


async def test_retries_count_toward_cap(conn):
    (pid,) = add(conn, 1)
    client = FakeClient(["bad", GOOD])
    await scorer(conn, client, max_calls=1).score_pending(10)
    assert len(client.calls) == 1
    assert status(conn, pid)["status"] == "score_error"


async def test_feedback_examples_injected(conn):
    ids = add(conn, 2)
    db.set_reaction(conn, ids[0], "like", NOW)
    client = FakeClient([GOOD])
    await scorer(conn, client).score_pending(1)
    system = client.calls[0]["system"]
    assert len(system) == 2 and "Liked by the user" in system[1]["text"]


def test_sqlite_row_factory(conn):
    assert conn.row_factory is sqlite3.Row
