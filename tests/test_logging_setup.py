"""FIX-16: SDK loggers never emit request bodies, even at LOG_LEVEL=DEBUG (D-141)."""

import json
import logging

import httpx2
import pytest
import test_privacy_sweep as ps
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from src import db
from src.bot.outreach_handlers import cmd_prepare, ingest_cv_text
from src.cv import store
from src.llm.budget import LlmBudget
from src.llm.json_call import call_json
from src.logging_setup import QUIET_LOGGERS, configure_logging
from src.models import Kind, Posting
from src.outreach import draft as dr
from src.outreach import prepare as prep


class Out(BaseModel):
    answer: str


def real_client(replies, seen):
    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        text = replies[min(len(seen), len(replies)) - 1]
        return httpx2.Response(
            200,
            json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )  # fmt: skip

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)


@pytest.fixture(autouse=True)
def _restore_logging():
    names = (*QUIET_LOGGERS, "anthropic._base_client", "src.canary")
    saved = {n: logging.getLogger(n).level for n in names}
    root = logging.getLogger().level
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    prep.register_hooks()
    dr.register_hooks()
    prep._locks.clear()
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)
    logging.getLogger().setLevel(root)
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()


def test_quiet_loggers_pinned_at_debug(caplog):
    configure_logging("DEBUG")
    for n in ("anthropic", "httpx", "httpx2", "httpcore", "aiogram.event", "telethon"):
        assert logging.getLogger(n).getEffectiveLevel() == logging.WARNING
    assert not logging.getLogger("anthropic._base_client").isEnabledFor(logging.DEBUG)


def test_app_debug_logs_still_appear(caplog):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    logging.getLogger("src.canary").debug("app-debug-visible")
    assert "app-debug-visible" in caplog.text


async def test_sdk_canary_leaks_without_pin(caplog):
    """Control: the SDK really does log the body at DEBUG, so the next test is not vacuous."""
    for n in QUIET_LOGGERS:
        logging.getLogger(n).setLevel(logging.NOTSET)  # undo pins applied by other tests
    caplog.set_level(logging.DEBUG)
    seen = []
    client = real_client([json.dumps({"answer": "x"})], seen)
    await client.messages.create(
        model="m", max_tokens=5, messages=[{"role": "user", "content": "CANARY-CV-TEXT"}]
    )
    assert "CANARY-CV-TEXT" in caplog.text


async def test_call_json_real_client_no_canary(tmp_path, caplog):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    conn = db.connect(tmp_path / "o.db")
    db.migrate(conn)
    seen = []
    client = real_client([json.dumps({"answer": "CANARY-REPLY"})], seen)
    res = await call_json(
        client, model="m", system_blocks=[{"type": "text", "text": "sys"}],
        user_text="<cv>CANARY-CV-TEXT</cv>", schema=Out, max_tokens=50,
        budget=LlmBudget(conn, ps.CLOCK, 10), purpose="t",
    )  # fmt: skip
    assert res.value.answer == "CANARY-REPLY"
    assert b"CANARY-CV-TEXT" in seen[0].content
    assert "CANARY-CV-TEXT" not in caplog.text
    assert "CANARY-REPLY" not in caplog.text
    assert "test-key" not in caplog.text


async def test_prepare_real_client_no_canary(tmp_path, caplog):
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    conn = db.connect(tmp_path / "o.db")
    db.migrate(conn)
    p = Posting(
        source="t", external_id="1", title=ps.BAIT["title"], company="Acme Fake",
        url="https://acme-fake.test/jobs/1", kind=Kind.JOB,
        description=ps.BAIT["description"] + " CANARY-POSTING",
    )  # fmt: skip
    db.upsert_posting(conn, p, "fp" + p.id, ps.NOW)
    seen = []
    client = real_client([ps.PARSE, ps.TAILOR], seen)
    msgs = ps.Msg()
    await ingest_cv_text(msgs, conn, ps.CLOCK, ps.CFG, client, ps.CV)
    await cmd_prepare(msgs, conn, ps.CLOCK, ps.CFG, client, p.id, None)
    assert len(seen) == 3
    blob = b"".join(r.content for r in seen)
    assert b"CANARY-CV" in blob and b"CANARY-POSTING" in blob
    for c in ("CANARY-CV", "CANARY-POSTING", "CANARY-REPLY"):
        assert c not in caplog.text


# ---- FIX-29: ANTHROPIC_LOG=debug cannot undo the pin (D-154) ----


def test_anthropic_log_env_dropped_and_pin_holds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    configure_logging("DEBUG")
    import os

    assert "ANTHROPIC_LOG" not in os.environ
    from anthropic._utils import _logs

    _logs.setup_logging()  # what an SDK import would run
    for n in QUIET_LOGGERS:
        assert logging.getLogger(n).getEffectiveLevel() == logging.WARNING


def test_fresh_process_import_order_with_anthropic_log_debug():
    """In a new interpreter the SDK is not yet imported when configure_logging runs."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import logging;"
        "from src.logging_setup import configure_logging;"
        "configure_logging('DEBUG');"
        "import anthropic;"
        "print(logging.getLogger('anthropic').level, logging.getLogger('httpx2').level)"
    )
    env = {**os.environ, "ANTHROPIC_LOG": "debug", "PYTHONPATH": str(Path(__file__).parent.parent)}
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.split() == ["30", "30"]


async def test_call_json_no_canary_with_anthropic_log_debug(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    conn = db.connect(tmp_path / "o.db")
    db.migrate(conn)
    seen = []
    client = real_client([json.dumps({"answer": "CANARY-REPLY"})], seen)
    await call_json(
        client, model="m", system_blocks=[{"type": "text", "text": "sys"}],
        user_text="<cv>CANARY-CV-TEXT</cv>", schema=Out, max_tokens=50,
        budget=LlmBudget(conn, ps.CLOCK, 10), purpose="t",
    )  # fmt: skip
    assert b"CANARY-CV-TEXT" in seen[0].content
    assert "CANARY-CV-TEXT" not in caplog.text and "CANARY-REPLY" not in caplog.text
