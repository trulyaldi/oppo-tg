"""All implemented sources through the full pipeline, with fixtures and fakes (T29, D-52)."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

import src.sources  # noqa: F401
from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.delivery.alerts import send_alerts
from src.delivery.digest import send_digest
from src.http import build_client
from src.pipeline.run import collect_all, score_pending
from src.sources.base import MIN_SOURCE_FAMILIES, count_report

ROOT = Path(__file__).resolve().parent.parent
FX = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
REPLY = json.dumps(
    {"score": 8.0, "reason": "Good fit", "deadline": None, "red_flags": [], "tags": []}
)


def _read(name: str) -> str:
    return (FX / name).read_text()


BODIES = {
    "boards-api.greenhouse.io": _read("greenhouse_jobs.json"),
    "api.lever.co": _read("lever_postings.json"),
    "api.ashbyhq.com": _read("ashby_board.json"),
    "jobicy.com": _read("jobicy_jobs.json"),
    "www.arbeitnow.com": _read("arbeitnow_api.json"),
    "www.mlh.com": _read("mlh_events.html"),
    "hnrss.org": _read("rss_sample.xml"),
    "www.nsf.gov": _read("rss_nsf.xml"),
}


def handler(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    host = req.url.host
    if host == "hn.algolia.com":
        name = "hn_search.json" if req.url.path.endswith("search_by_date") else "hn_item.json"
        return httpx.Response(200, text=_read(name))
    if host in BODIES:
        return httpx.Response(200, text=BODIES[host])
    if host in {"hh.ru", "api.hh.ru"}:
        raise AssertionError("credential-gated hh must not be called: " + str(req.url))
    # pagewatch pages (many hosts)
    return httpx.Response(200, text=_read("page_v1.html"), headers={"content-type": "text/html"})


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
        return SimpleNamespace(message_id=900 + len(self.sent))


def test_count_report_has_six_counted_families():
    # Deliberately reads the real config.toml: DoD guard for >= 6 sources.
    cfg = AppConfig.load(ROOT / "config.toml")
    report = count_report(Settings.from_env(ENV), cfg)
    assert len(report.counted) >= MIN_SOURCE_FAMILIES


async def test_all_sources_end_to_end():
    conn = db.connect(":memory:")
    db.migrate(conn)
    clock = FixedClock(NOW)
    settings = Settings.from_env(ENV)
    cfg = AppConfig.load(ROOT / "tests" / "fixtures" / "config_repo_snapshot.toml")
    report = count_report(settings, cfg)
    assert len(report.counted) >= MIN_SOURCE_FAMILIES
    counted = {n for names in report.counted.values() for n in names}

    async def nosleep(_):
        pass

    http = build_client(conn, clock, transport=httpx.MockTransport(handler), sleep=nosleep)
    stats = await collect_all(conn, http, settings, cfg, clock)
    await http.aclose()

    assert stats["failed"] == 0  # no source exception escaped, no source failed
    runs = {r["source"]: r for r in db.last_source_runs(conn)}
    assert counted <= set(runs)
    for name in counted:
        assert runs[name]["ok"] == 1, name
        assert runs[name]["n_items"] >= 1, name
    assert "hh" not in runs and "telegram_channels" not in runs and "devpost" not in runs

    client = FakeAnthropic()
    await score_pending(conn, client, "test-model", clock, 1000)
    n_scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    assert n_scored >= 1 and client.calls >= n_scored

    bot = FakeBot()
    n_alerts = await send_alerts(bot, conn, settings, cfg, clock)
    n_digest = await send_digest(bot, conn, settings, cfg, clock)
    rows = conn.execute("SELECT posting_id, kind FROM deliveries").fetchall()
    assert len({r["posting_id"] for r in rows}) == len(rows) == n_alerts + n_digest
    assert n_digest >= 1
    assert all(m["chat_id"] == 42 or m["chat_id"] == "42" for m in bot.sent)


def test_help_lists_probe_and_commands():
    r = subprocess.run(
        [sys.executable, "-m", "src.main", "--help"], cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0
    for cmd in ("run", "collect", "digest", "once", "probe"):
        assert cmd in r.stdout
