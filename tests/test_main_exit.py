"""S2: `collect`/`once` send a Telegram error and exit non-zero when scoring fails entirely."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import aiogram
import anthropic
import pytest

import src.main as main_mod
from src import db
from src.config import Settings
from src.models import Kind, Posting
from src.pipeline.dedupe import fingerprint

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
GOOD = json.dumps({"score": 8.0, "reason": "ok", "deadline": None, "red_flags": [], "tags": []})
ROOT = Path(__file__).resolve().parent.parent
SENT: list = []


class FakeBot:
    def __init__(self, token):
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        pass

    async def send_message(self, chat_id, text, **kw):
        SENT.append((chat_id, text))


def make_client(reply):
    class Client:
        def __init__(self, api_key=None):
            self.messages = self

        async def create(self, **kw):
            if isinstance(reply, Exception):
                raise reply
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])

    return Client


@pytest.fixture
def setup(tmp_path, monkeypatch):
    SENT.clear()
    path = tmp_path / "o.db"
    conn = db.connect(path)
    db.migrate(conn)
    p = Posting(
        source="t",
        external_id="1",
        title="Quant intern",
        company="Acme",
        url="https://example.com/x",
        kind=Kind.INTERNSHIP,
    )
    db.upsert_posting(conn, p, fingerprint(p), NOW)
    conn.close()

    async def fake_collect(*a, **k):
        return {}

    class FakeHttp:
        async def aclose(self):
            pass

    async def fake_alerts(*a, **k):
        return 0

    monkeypatch.setattr(main_mod, "collect_all", fake_collect)
    monkeypatch.setattr(main_mod, "build_client", lambda *a, **k: FakeHttp())
    monkeypatch.setattr(aiogram, "Bot", FakeBot)
    import src.delivery.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "send_alerts", fake_alerts)
    return Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "1:t",
            "TELEGRAM_CHAT_ID": "42",
            "ANTHROPIC_API_KEY": "k",
            "DB_PATH": str(path),
            "OPPO_CONFIG": str(ROOT / "config.toml"),
        }
    )


async def test_collect_total_failure_alerts_and_exits_nonzero(setup, monkeypatch):
    monkeypatch.setattr(anthropic, "AsyncAnthropic", make_client(RuntimeError("401 bad key")))
    assert await main_mod._run("collect", setup) == 1
    assert len(SENT) == 1
    chat_id, text = SENT[0]
    assert chat_id == "42" or chat_id == 42
    assert "ERROR: scoring failed" in text and "401 bad key" in text


async def test_collect_success_exits_zero_without_alert(setup, monkeypatch):
    monkeypatch.setattr(anthropic, "AsyncAnthropic", make_client(GOOD))
    assert await main_mod._run("collect", setup) == 0
    assert SENT == []


async def test_nothing_to_score_exits_zero(setup, monkeypatch):
    conn = db.connect(setup.db_path)
    conn.execute("UPDATE postings SET status='irrelevant'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(anthropic, "AsyncAnthropic", make_client(RuntimeError("x")))
    assert await main_mod._run("collect", setup) == 0 and SENT == []


def test_main_returns_the_exit_code(monkeypatch):
    async def fake(command, settings):
        return 1

    monkeypatch.setattr(main_mod, "_run", fake)
    env = {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
    assert main_mod.main(["collect"], env) == 1
