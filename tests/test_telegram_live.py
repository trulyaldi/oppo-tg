"""Telethon live listener with a fake client and fake NewMessage events (FC-24). No network."""

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src import db
from src.bot.handlers import cmd_channels, render_channels
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.pipeline.run import collect_tier, pipeline_lock
from src.sources import telegram_channels as tgc
from src.sources.base import SourceContext, Target, run_target
from src.sources.telegram_channels import TelegramChannelsSource, set_listener
from src.sources.telegram_live import TelegramListener

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
CLOCK = FixedClock(NOW)
ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "ANTHROPIC_API_KEY": "k"}
CREDS = {"TG_API_ID": "123", "TG_API_HASH": "fakehash"}
SETTINGS = Settings.from_env({**ENV, **CREDS})


def cfg_with(chs=("job_kz", "intern_kz"), **extra):
    section = {"enabled": True, "channels": list(chs), **extra}
    return AppConfig.from_dict({"sources": {"telegram_channels": section}})


class FakeClient:
    def __init__(self, joined=("job_kz", "intern_kz"), authorized=True):
        self.joined = list(joined)
        self.authorized = authorized
        self.connected = False
        self.handlers = []
        self.removed = []
        self.requests = []
        self.disconnects = 0
        self.history = {}

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False
        self.disconnects += 1

    async def is_user_authorized(self):
        return self.authorized

    async def iter_dialogs(self):
        for name in self.joined:
            yield SimpleNamespace(entity=SimpleNamespace(username=name))

    def add_event_handler(self, cb, event):
        self.handlers.append((cb, event))

    def remove_event_handler(self, cb):
        self.removed.append(cb)
        self.handlers = [h for h in self.handlers if h[0] != cb]

    async def __call__(self, request):
        self.requests.append(request)
        self.joined.append(request.channel)

    async def iter_messages(self, channel, limit=None, min_id=0):
        for mid, text in self.history.get(channel, []):
            if mid > min_id:
                yield SimpleNamespace(id=mid, message=text, date=NOW)


def event(channel, mid, text, username=None):
    chat = SimpleNamespace(username=username or channel)
    msg = SimpleNamespace(id=mid, message=text, date=NOW)
    return SimpleNamespace(chat=chat, message=msg)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture(autouse=True)
def _no_listener():
    set_listener(None)
    yield
    set_listener(None)


def make(conn, client, cfg=None, on_new=None, settings=SETTINGS, **kw):
    return TelegramListener(
        settings,
        cfg or cfg_with(),
        conn,
        client_factory=lambda s: client,
        on_new=on_new,
        clock=CLOCK,
        require_session_file=False,
        **kw,
    )


def last_id(conn, channel):
    return int(db.get_state(conn, f"telegram_channels:last_id:{channel}", "0"))


# ----- start / missing credentials -----


async def test_missing_credentials_no_crash_logs_once(conn, caplog):
    def boom(_):
        raise AssertionError("no client without credentials")

    lst = TelegramListener(
        Settings.from_env(ENV),
        cfg_with(),
        conn,
        client_factory=boom,
        require_session_file=False,
    )
    with caplog.at_level(logging.WARNING):
        assert await lst.start() is False
    assert [r for r in caplog.records if "TG_API_ID" in r.getMessage()] != []
    assert len([r for r in caplog.records if "not started" in r.getMessage()]) == 1
    assert "TG_API_ID" in lst.reason
    assert lst.live_channels() == set()


async def test_missing_session_file_is_a_clean_false(conn, tmp_path):
    env = {**ENV, **CREDS, "TG_SESSION_PATH": str(tmp_path / "none.session")}
    lst = TelegramListener(Settings.from_env(env), cfg_with(), conn, client_factory=lambda s: 1 / 0)
    assert await lst.start() is False
    assert "session" in lst.reason


async def test_unauthorized_session_not_started_and_disconnected(conn):
    client = FakeClient(authorized=False)
    lst = make(conn, client)
    assert await lst.start() is False
    assert client.disconnects == 1 and lst.client is None
    assert "not authorized" in lst.reason


async def test_connect_error_does_not_crash(conn):
    class Boom(FakeClient):
        async def connect(self):
            raise OSError("net down")

    lst = make(conn, Boom())
    assert await lst.start() is False
    assert not lst.started


async def test_subscribes_only_to_joined_channels(conn):
    client = FakeClient(joined=["job_kz", "other"])
    lst = make(conn, client)
    assert await lst.start() is True
    assert lst.live_channels() == {"job_kz"}
    assert len(client.handlers) == 1
    assert client.requests == []  # auto_join is off: nothing joined


async def test_auto_join_true_joins_missing_channels(conn):
    client = FakeClient(joined=["job_kz"])
    lst = make(conn, client, cfg_with(auto_join=True))
    await lst.start()
    assert len(client.requests) == 1
    assert lst.live_channels() == {"job_kz", "intern_kz"}


# ----- events -----


async def test_event_stores_with_baseline_zero_then_advances_last_id(conn):
    lst = make(conn, FakeClient())
    await lst.start()
    await lst._on_event(event("job_kz", 11, "Intern ML\nCompany: Acme"))
    row = conn.execute("SELECT * FROM postings").fetchone()
    assert row["source"] == "telegram_channels" and row["target"] == "job_kz"
    assert row["baseline"] == 0 and row["external_id"] == "job_kz:11"
    assert last_id(conn, "job_kz") == 11


async def test_last_id_not_advanced_when_store_fails(conn, monkeypatch):
    lst = make(conn, FakeClient())
    await lst.start()

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "upsert_posting", boom)
    await lst._on_event(event("job_kz", 12, "Data analyst\nCompany: X"))
    assert last_id(conn, "job_kz") == 0  # retried by polling later


async def test_ads_and_foreign_channels_are_ignored_but_ads_advance(conn):
    lst = make(conn, FakeClient())
    await lst.start()
    await lst._on_event(event("job_kz", 5, "#реклама купи"))
    await lst._on_event(event("elsewhere", 6, "Python dev"))
    assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
    assert last_id(conn, "job_kz") == 5
    assert last_id(conn, "elsewhere") == 0


async def test_username_case_maps_to_configured_spelling(conn):
    lst = make(conn, FakeClient())
    await lst.start()
    await lst._on_event(event("job_kz", 3, "Backend dev", username="JOB_KZ"))
    assert conn.execute("SELECT target FROM postings").fetchone()[0] == "job_kz"


async def test_last_id_never_moves_backwards(conn):
    lst = make(conn, FakeClient())
    await lst.start()
    await lst._on_event(event("job_kz", 20, "Dev one"))
    await lst._on_event(event("job_kz", 15, "Dev two"))
    assert last_id(conn, "job_kz") == 20


async def test_debounced_flush_batches_and_store_never_waits_for_lock(conn):
    gate = asyncio.Event()
    batches = []

    async def sleep(_):
        await gate.wait()

    async def on_new(batch):
        async with pipeline_lock():
            batches.append([p.external_id for p in batch])

    lst = make(conn, FakeClient(), on_new=on_new, sleep=sleep)
    await lst.start()
    async with pipeline_lock():  # a tier job holds the lock
        await asyncio.wait_for(lst._on_event(event("job_kz", 1, "Dev one")), 1)
        await asyncio.wait_for(lst._on_event(event("job_kz", 2, "Dev two")), 1)
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 2
        gate.set()
        await asyncio.sleep(0.01)
        assert batches == []  # flush is waiting on the lock, storing was not
    await lst._flush_task
    assert batches == [["job_kz:1", "job_kz:2"]]


async def test_flush_error_is_contained(conn):
    async def sleep(_):
        return None

    async def on_new(batch):
        raise RuntimeError("scoring down")

    lst = make(conn, FakeClient(), on_new=on_new, sleep=sleep)
    await lst.start()
    await lst._on_event(event("job_kz", 1, "Dev one"))
    await lst._flush_task  # does not raise


def test_flush_default_is_30_seconds(conn):
    assert make(conn, FakeClient()).flush_s == 30
    assert make(conn, FakeClient(), cfg_with(live_flush_s=5)).flush_s == 5


# ----- reload / stop -----


async def test_reload_after_channels_change(conn):
    client = FakeClient(joined=["job_kz", "intern_kz", "new_chan"])
    lst = make(conn, client)
    await lst.start()
    db.set_tg_channel(conn, "new_chan", "added", NOW)
    db.set_tg_channel(conn, "job_kz", "removed", NOW)
    await lst.reload()
    assert lst.live_channels() == {"intern_kz", "new_chan"}
    assert len(client.handlers) == 1 and client.removed  # old handler replaced


async def test_reload_noop_when_not_started(conn):
    lst = make(conn, FakeClient(authorized=False))
    await lst.start()
    await lst.reload()
    assert lst.live_channels() == set()


async def test_stop_disconnects_and_cancels_flush(conn):
    async def sleep(_):
        await asyncio.sleep(3600)

    client = FakeClient()
    lst = make(conn, client, on_new=lambda b: asyncio.sleep(0), sleep=sleep)
    await lst.start()
    await lst._on_event(event("job_kz", 1, "Dev one"))
    task = lst._flush_task
    await lst.stop()
    assert client.disconnects == 1 and task.cancelled()
    assert lst.live_channels() == set()


async def test_disconnected_client_means_no_live_channels(conn):
    client = FakeClient()
    lst = make(conn, client)
    await lst.start()
    client.connected = False
    assert lst.live_channels() == set()


# ----- polling interplay -----


def ctx_for(conn, cfg):
    return SourceContext(
        http=build_client(conn, CLOCK),
        settings=SETTINGS,
        cfg=cfg,
        clock=CLOCK,
        log=logging.getLogger("t"),
    )


async def test_polling_skips_live_channels_and_covers_all_when_disconnected(conn):
    client = FakeClient(joined=["job_kz"])
    lst = make(conn, client)
    await lst.start()
    set_listener(lst)
    src = TelegramChannelsSource(require_session_file=False)
    ctx = ctx_for(conn, cfg_with())
    assert [t.key for t in src.targets(ctx)] == ["intern_kz"]  # job_kz is live
    client.connected = False
    assert [t.key for t in src.targets(ctx)] == ["job_kz", "intern_kz"]


async def test_polling_reuses_listener_client_and_never_disconnects_it(conn):
    client = FakeClient(joined=["job_kz"])
    client.history = {"intern_kz": [(7, "Intern data\nCompany: Acme")]}
    lst = make(conn, client)
    await lst.start()
    set_listener(lst)

    def boom(_):
        raise AssertionError("a second client must not be built")

    src = TelegramChannelsSource(client_factory=boom, require_session_file=False)
    items = await src.fetch_target(
        ctx_for(conn, cfg_with()), Target("telegram_channels", "intern_kz")
    )
    assert [p.external_id for p in items] == ["intern_kz:7"]
    assert client.disconnects == 0 and client.connected


async def test_polling_reconnects_shared_client_when_listener_disconnected(conn):
    client = FakeClient(joined=["job_kz"])
    client.history = {"job_kz": [(9, "Dev job")]}
    lst = make(conn, client)
    await lst.start()
    set_listener(lst)
    client.connected = False
    src = TelegramChannelsSource(require_session_file=False)
    items = await src.fetch_target(ctx_for(conn, cfg_with()), Target("telegram_channels", "job_kz"))
    assert len(items) == 1 and client.connected and client.disconnects == 0


# ----- post-store hook (polling advances last_id only after the store) -----


async def test_polling_advances_last_id_only_through_post_store_hook(conn):
    client = FakeClient()
    client.history = {"job_kz": [(4, "Dev one"), (8, "Dev two")]}
    src = TelegramChannelsSource(client_factory=lambda s: client, require_session_file=False)
    ctx = ctx_for(conn, cfg_with())
    tr = await run_target(src, ctx, Target("telegram_channels", "job_kz"))
    assert tr.ok and len(tr.items) == 2
    assert last_id(conn, "job_kz") == 0  # not before the store
    assert tr.on_stored is not None
    tr.on_stored()
    assert last_id(conn, "job_kz") == 8


async def test_failed_target_leaves_no_hook(conn):
    client = FakeClient(authorized=False)
    src = TelegramChannelsSource(client_factory=lambda s: client, require_session_file=False)
    ctx = ctx_for(conn, cfg_with())
    tr = await run_target(src, ctx, Target("telegram_channels", "job_kz"))
    assert not tr.ok and tr.on_stored is None and ctx.after_store == {}


async def test_collector_runs_hook_after_store_and_records_unauthorized_as_failed(
    conn, monkeypatch
):
    client = FakeClient()
    client.history = {"job_kz": [(4, "Dev one")]}
    monkeypatch.setattr(tgc, "default_client_factory", lambda s: client)
    cfg = cfg_with(("job_kz",))
    http = build_client(conn, CLOCK)
    settings = SETTINGS
    monkeypatch.setattr(tgc, "session_exists", lambda s: True)
    stats = await collect_tier(conn, http, settings, cfg, CLOCK, "tg")
    assert stats["new"] == 1 and last_id(conn, "job_kz") == 4
    # unauthorized: the run is recorded failed, not ok (second D-172 defect)
    client.authorized = False
    conn.execute("DELETE FROM target_state")
    stats = await collect_tier(conn, http, settings, cfg, CLOCK, "tg")
    assert stats["failed"] == 1
    run = conn.execute(
        "SELECT ok, error FROM source_runs WHERE source='telegram_channels' ORDER BY id DESC"
    ).fetchone()
    assert run["ok"] == 0 and "not authorized" in run["error"]


# ----- /channels status -----


async def test_channels_status_shows_live(conn):
    client = FakeClient(joined=["job_kz"])
    lst = make(conn, client)
    await lst.start()
    set_listener(lst)
    lines = render_channels(conn, SETTINGS, cfg_with())
    assert "@job_kz [config] live" in lines[0]
    assert "live" not in lines[1]


async def test_channels_command_reloads_listener(conn):
    client = FakeClient(joined=["job_kz", "intern_kz", "fresh_kz"])
    lst = make(conn, client)
    await lst.start()
    set_listener(lst)

    class Msg:
        def __init__(self):
            self.sent = []

        async def answer(self, text, parse_mode=None, **kw):
            self.sent.append(text)

    m = Msg()
    cfg = cfg_with()
    await cmd_channels(m, conn, SETTINGS, cfg, CLOCK, "add fresh_kz")
    assert "fresh_kz" in lst.live_channels()


async def test_live_subscribed_channels_get_baselined_target_state(conn):
    client = FakeClient(joined=["job_kz"])
    lst = make(conn, client)
    assert await lst.start() is True
    live = db.get_target_state(conn, "telegram_channels", "job_kz")
    assert live is not None and live["baselined_at"] and live["last_ok_at"]
    # a channel the account has not joined stays on polling and has no state row
    assert db.get_target_state(conn, "telegram_channels", "intern_kz") is None


async def test_live_state_keeps_an_existing_baseline_time(conn):
    first = datetime(2026, 11, 1, tzinfo=UTC)
    db.record_target_attempt(conn, "telegram_channels", "job_kz", first, ok=True)
    db.mark_baselined(conn, "telegram_channels", "job_kz", first)
    lst = make(conn, FakeClient(joined=["job_kz"]))
    await lst.start()
    row = db.get_target_state(conn, "telegram_channels", "job_kz")
    assert row["baselined_at"].startswith("2026-11-01")


async def test_kf17_tg_tier_runs_only_telegram_channels(conn, monkeypatch):
    client = FakeClient()
    client.history = {"job_kz": [(4, "Dev one")]}
    monkeypatch.setattr(tgc, "default_client_factory", lambda s: client)
    monkeypatch.setattr(tgc, "session_exists", lambda s: True)
    cfg = AppConfig.from_dict(
        {
            "sources": {
                "telegram_channels": {"enabled": True, "channels": ["job_kz"]},
                "greenhouse": {"enabled": True, "boards": ["a"]},
            }
        }
    )
    http = build_client(conn, CLOCK)
    await collect_tier(conn, http, SETTINGS, cfg, CLOCK, "tier2")  # nothing is tg-tier here
    assert last_id(conn, "job_kz") == 0
    await collect_tier(conn, http, SETTINGS, cfg, CLOCK, "tg")
    assert last_id(conn, "job_kz") == 4
    sources = {r["source"] for r in conn.execute("SELECT source FROM target_state")}
    assert sources == {"telegram_channels"}
