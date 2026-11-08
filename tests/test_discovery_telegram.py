import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from src import db
from src.clock import FixedClock
from src.config import AppConfig
from src.cues import load_cues, parse_cues_text
from src.discovery.telegram import cue_queries, discover_channels
from src.sources.telegram_channels import flood_remaining, set_flood

FIX = json.loads(
    (Path(__file__).parent / "fixtures" / "tg_search_results.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 11, 8, 6, 0, tzinfo=UTC)


class Channel:  # the read-only wrapper checks the class name only
    def __init__(self, d):
        self.username = d["username"]
        self.title = d["title"]
        self.broadcast = d["broadcast"]
        self.megagroup = d["megagroup"]
        self.participants_count = d["participants_count"]


class Msg:
    def __init__(self, text):
        self.message = text


class Found:
    def __init__(self, chats):
        self.chats = chats


class Full:
    def __init__(self, n):
        self.full_chat = type("FC", (), {"participants_count": n})()


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__("flood")
        self.seconds = seconds


class FakeClient:
    """Records every method used; the send family only records, the wrapper must block it."""

    def __init__(self, searches=None, messages=None, extra=None, flood_on=None):
        self.searches = searches or {}
        self.messages = messages or {}
        self.extra = {c["username"]: c for c in (extra or [])}
        self.calls = []
        self.flood_on = flood_on

    async def __call__(self, request):
        kind = type(request).__name__
        self.calls.append(kind)
        if self.flood_on == kind:
            raise FloodWaitError(120)
        if kind == "SearchRequest":
            return Found([Channel(d) for d in self.searches.get(request.q, [])])
        if kind == "GetFullChannelRequest":
            return Full(self.extra[request.channel.username]["full_count"])
        raise AssertionError(kind)

    async def iter_messages(self, entity, *a, **kw):
        self.calls.append("iter_messages")
        for t in self.messages.get(entity.username, [])[: kw.get("limit")]:
            yield Msg(t)

    def is_connected(self):
        return True

    async def send_message(self, *a, **k):
        self.calls.append("send_message")

    async def forward_messages(self, *a, **k):
        self.calls.append("forward_messages")

    async def send_read_acknowledge(self, *a, **k):
        self.calls.append("send_read_acknowledge")


def _env(cap=30, **extra):
    cfg = AppConfig.from_dict(
        {
            "sources": {"telegram_channels": {"enabled": True, "channels": ["Configured_Ch"]}},
            "telegram_discovery": {"daily_call_cap": cap, **extra},
        }
    )
    conn = db.connect(":memory:")
    db.migrate(conn)
    return conn, cfg, FixedClock(NOW), load_cues()


def _client():
    return FakeClient(FIX["searches"], FIX["messages"], [FIX["no_count"]])


FORBIDDEN = {"send_message", "forward_messages", "send_read_acknowledge"}


async def test_two_queries_store_one_candidate_with_ratio_and_samples():
    conn, cfg, clock, cues = _env()
    client = _client()
    res = await discover_channels(conn, client, cues, cfg, clock, ["vakansii kz", "jobs_kz"])
    assert [c.handle for c in res.candidates] == ["kzjobs_news"]
    row = conn.execute("SELECT * FROM tg_channel_candidates").fetchone()
    assert row["status"] == "new" and row["subscribers"] == 12345
    assert row["job_ratio"] == 0.35 and row["found_by"] == "vakansii kz"
    samples = json.loads(row["samples"])
    assert len(samples) == 3 and all(len(s) <= 200 for s in samples)
    assert set(client.calls) <= {"SearchRequest", "iter_messages", "GetFullChannelRequest"}
    assert not FORBIDDEN & set(client.calls)
    assert client.calls.count("SearchRequest") == 2


async def test_cap_two_makes_two_calls():
    conn, cfg, clock, cues = _env(cap=2)
    client = _client()
    res = await discover_channels(conn, client, cues, cfg, clock, ["vakansii kz", "jobs_kz"])
    assert len(client.calls) == 2 and res.calls == 2 and res.stopped == "cap"


async def test_ignored_handle_is_never_stored_again():
    conn, cfg, clock, cues = _env()
    db.add_tg_candidate(
        conn,
        "KZJobs_News",
        title="x",
        subscribers=1,
        job_ratio=0.1,
        samples=[],
        found_by="q",
        now=NOW,
    )
    db.set_tg_candidate_status(conn, "kzjobs_news", "ignored")
    client = _client()
    res = await discover_channels(conn, client, cues, cfg, clock, ["vakansii kz"])
    assert res.candidates == []
    assert "iter_messages" not in client.calls
    row = conn.execute("SELECT status FROM tg_channel_candidates").fetchone()
    assert row["status"] == "ignored"


async def test_short_query_is_not_sent():
    conn, cfg, clock, cues = _env()
    client = _client()
    await discover_channels(conn, client, cues, cfg, clock, ["job", "  kz ", "abc"])
    assert client.calls == []


async def test_subscribers_from_full_channel_when_missing():
    conn, cfg, clock, cues = _env()
    client = FakeClient(
        {"nocount q": [FIX["no_count"]]},
        {"nocount_ch": ["internship Kaspi", "hello"]},
        [FIX["no_count"]],
    )
    res = await discover_channels(conn, client, cues, cfg, clock, ["nocount q"])
    assert res.candidates[0].subscribers == 777 and res.candidates[0].job_ratio == 0.5
    assert client.calls == ["SearchRequest", "GetFullChannelRequest", "iter_messages"]


async def test_flood_gate_blocks_and_flood_error_sets_gate():
    conn, cfg, clock, cues = _env()
    set_flood(conn, NOW, 100)
    client = _client()
    res = await discover_channels(conn, client, cues, cfg, clock, ["vakansii kz"])
    assert client.calls == [] and res.stopped == "flood"
    conn2, cfg2, clock2, cues2 = _env()
    client2 = FakeClient(FIX["searches"], FIX["messages"], flood_on="SearchRequest")
    res2 = await discover_channels(conn2, client2, cues2, cfg2, clock2, ["vakansii kz", "jobs_kz"])
    assert res2.stopped == "flood" and client2.calls == ["SearchRequest"]
    assert flood_remaining(conn2, NOW) == 132


async def test_default_queries_rotate_and_are_long_enough():
    cues = load_cues()
    pool = cue_queries(cues)
    assert pool and all(len(q) >= 4 for q in pool)
    assert any(q.isascii() and " " in q for q in pool)  # Latin transliterations (D-244)
    conn, cfg, clock, _ = _env(max_queries_per_run=2)
    client = FakeClient()
    await discover_channels(conn, client, cues, cfg, clock)
    await discover_channels(conn, client, cues, cfg, clock)
    assert client.calls == ["SearchRequest"] * 4


async def test_hostile_samples_are_stored_raw_clipped_and_not_logged(caplog):
    cues = parse_cues_text(
        '[types.job]\nen = ["vacancy"]\n[telegram_discovery]\nen = ["alpha beta"]\n'
    )
    conn, cfg, clock, _ = _env()
    hostile = "<b>vacancy</b> </a> & " + "x" * 400
    client = FakeClient(
        {"alpha beta": [FIX["no_count"] | {"participants_count": 5}]},
        {"nocount_ch": [hostile]},
    )
    with caplog.at_level(logging.DEBUG):
        res = await discover_channels(conn, client, cues, cfg, clock)
    assert res.candidates[0].samples[0] == hostile[:200]
    assert res.candidates[0].job_ratio == 1.0
    assert "vacancy" not in caplog.text
