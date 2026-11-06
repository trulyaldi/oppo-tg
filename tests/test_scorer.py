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
    assert row["base_score"] == 8.5 and row["model"] == "test-model" and row["prompt_version"]
    assert status(conn, pid)["status"] == "scored"
    kw = client.calls[0]
    assert kw["max_tokens"] == 400 and kw["model"] == "test-model"
    assert "temperature" not in kw  # removed from the current SDK/models (D-83)
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


# --- the call must fit the REAL anthropic SDK signature (D-83) ---------------


class SignatureCheckedClient(FakeClient):
    """Fake that rejects any keyword the installed SDK's create() would not accept."""

    async def create(self, **kw):
        import inspect

        from anthropic.resources.messages import AsyncMessages

        inspect.signature(AsyncMessages.create).bind(None, **kw)  # TypeError like the real one
        return await super().create(**kw)


async def test_call_matches_installed_sdk_signature(conn):
    (pid,) = add(conn, 1)
    client = SignatureCheckedClient([GOOD])
    await scorer(conn, client).score_pending(10)
    assert status(conn, pid)["status"] == "scored"
    assert set(client.calls[0]) <= {"model", "max_tokens", "system", "messages"}


def test_signature_check_would_have_caught_temperature():
    import inspect

    from anthropic.resources.messages import AsyncMessages

    sig = inspect.signature(AsyncMessages.create)
    with pytest.raises(TypeError, match="temperature"):
        sig.bind(None, model="m", max_tokens=1, messages=[], temperature=0)


async def test_rejected_keyword_marks_error_and_records_reason(conn):
    (pid,) = add(conn, 1)
    client = FakeClient([TypeError("create() got an unexpected keyword argument 'temperature'")])
    s = scorer(conn, client)
    stats = await s.score_pending(10)
    assert stats["errors"] == 1 and stats["scored"] == 0
    assert status(conn, pid)["status"] != "scored"
    assert "TypeError" in s.last_error and "temperature" in s.last_error


def test_default_model_is_a_known_sdk_model():
    import typing

    from anthropic.types import Model

    from src.config import Settings

    known = {a for a in typing.get_args(Model) if isinstance(a, str)} or {
        x for a in typing.get_args(Model) for x in typing.get_args(a)
    }
    assert (
        Settings.from_env(
            {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
        ).scoring_model
        in known
    )


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


# --- S1: run the Scorer through the REAL AsyncAnthropic client (no network) ----


def _real_client(handler):
    import httpx2 as httpx
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _message_json(text):
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


async def test_real_sdk_client_scores_through_mock_transport(conn):
    import httpx2 as httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=_message_json(GOOD))

    (pid,) = add(conn, 1)
    client = _real_client(handler)
    s = Scorer(client, "claude-haiku-4-5", conn, FixedClock(NOW), 10)
    stats = await s.score_pending(10)
    assert stats == {"scored": 1, "errors": 0, "skipped": 0}, s.last_error
    assert status(conn, pid)["status"] == "scored"
    path, body = seen[0]
    assert path == "/v1/messages"
    assert set(body) == {"model", "max_tokens", "system", "messages"}
    assert body["model"] == "claude-haiku-4-5"


async def test_real_sdk_client_rejects_unsupported_kwarg():
    import httpx2 as httpx

    client = _real_client(lambda r: httpx.Response(200, json=_message_json("x")))
    with pytest.raises(TypeError, match="temperature"):
        await client.messages.create(
            model="m", max_tokens=1, messages=[{"role": "user", "content": "x"}], temperature=0
        )


async def test_real_sdk_client_http_error_is_a_scoring_error(conn):
    import httpx2 as httpx

    def handler(request):
        return httpx.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "x"}}
        )

    (pid,) = add(conn, 1)
    s = Scorer(_real_client(handler), "claude-haiku-4-5", conn, FixedClock(NOW), 10)
    stats = await s.score_pending(10)
    assert stats["errors"] == 1 and "AuthenticationError" in s.last_error


async def test_new_fields_round_trip_to_db(conn):
    (pid,) = add(conn, 1)
    reply = json.dumps(
        {
            "score": 7,
            "reason": "ok",
            "employer_region": "kz",
            "region_confidence": 0.85,
            "employer_is_startup": True,
            "visa_barrier": False,
        }
    )
    await scorer(conn, FakeClient([reply])).score_pending(10)
    row = conn.execute("SELECT * FROM scores WHERE posting_id=?", (pid,)).fetchone()
    assert row["employer_region"] == "kz" and row["region_confidence"] == 0.85
    assert row["employer_is_startup"] == 1 and row["visa_barrier"] == 0


async def test_old_reply_stores_defaults(conn):
    (pid,) = add(conn, 1)
    await scorer(conn, FakeClient([GOOD])).score_pending(10)
    row = conn.execute("SELECT * FROM scores WHERE posting_id=?", (pid,)).fetchone()
    assert row["employer_region"] == "unknown" and row["region_confidence"] == 0
    assert row["employer_is_startup"] is None and row["visa_barrier"] is None


async def test_invalid_region_triggers_retry(conn):
    (pid,) = add(conn, 1)
    bad = json.dumps({"score": 7, "reason": "x", "employer_region": "europe"})
    client = FakeClient([bad, GOOD])
    await scorer(conn, client).score_pending(10)
    assert len(client.calls) == 2 and status(conn, pid)["status"] == "scored"
