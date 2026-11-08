import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.digest import EMPTY_TEXT, send_digest
from src.delivery.format import feedback_keyboard
from src.http import build_client
from src.main import main
from src.pipeline.run import collect_all, score_pending

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = (ROOT / "tests" / "fixtures" / "greenhouse_jobs.json").read_text()
NOW = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)
REPLY = json.dumps(
    {"score": 8.0, "reason": "Strong <fit> & more", "deadline": None, "red_flags": [], "tags": []}
)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}


class FakeAnthropic:
    def __init__(self):
        self.messages = self
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=REPLY)])


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return SimpleNamespace(message_id=500 + len(self.sent))


def handler(req: httpx.Request) -> httpx.Response:
    assert req.url.host == "boards-api.greenhouse.io", req.url
    if req.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    return httpx.Response(200, text=FIXTURE, headers={"content-type": "application/json"})


async def test_end_to_end_slice():
    conn = db.connect(":memory:")
    db.migrate(conn)
    clock = FixedClock(NOW)
    settings = Settings.from_env(ENV)
    cfg = AppConfig.from_dict(
        {
            "sources": {
                "greenhouse": {
                    "enabled": True,
                    "boards": ["jumptrading"],
                    "names": {"jumptrading": "Jump Trading"},
                }
            },
            "filters": {
                "max_age_days": 60,
            },
        }
    )

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    stats = await collect_all(conn, http, settings, cfg, clock)
    await http.aclose()
    assert stats["sources"] == 1 and stats["failed"] == 0 and stats["items"] == 5
    run = db.last_source_runs(conn)[0]
    assert run["source"] == "greenhouse" and run["ok"] == 1 and run["n_new"] == 5

    client = FakeAnthropic()
    await score_pending(conn, client, "test-model", clock, 100)
    n = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert n >= 1 and client.calls == n

    bot = FakeBot()
    assert await send_digest(bot, conn, settings, cfg, clock) == n
    assert len(bot.sent) == n + 1
    assert "Daily digest 2026-09-05" in bot.sent[0]["text"]
    delivered = {r["posting_id"]: r for r in conn.execute("SELECT * FROM deliveries").fetchall()}
    assert len(delivered) == n
    for m in bot.sent[1:]:
        assert m["parse_mode"] == "HTML"
        assert "Relevance 8/10: Strong &lt;fit&gt; &amp; more" in m["text"]
        assert "— Jump Trading" in m["text"]
        kb = m["reply_markup"]
        pid = kb.inline_keyboard[0][0].callback_data.split(":")[2]
        assert pid in delivered
        assert kb == feedback_keyboard(pid)

    bot2 = FakeBot()
    assert await send_digest(bot2, conn, settings, cfg, clock) == 0
    assert [m["text"] for m in bot2.sent] == [EMPTY_TEXT]
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == n


def test_cli_help_exits_zero():
    r = subprocess.run(
        [sys.executable, "-m", "src.main", "--help"], cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0 and "collect" in r.stdout and "once" in r.stdout


def test_main_missing_env(capsys):
    assert main(["collect"], environ={}) != 0
    err = capsys.readouterr().err
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY"):
        assert name in err
