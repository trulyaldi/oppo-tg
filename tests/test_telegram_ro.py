"""Read-only Telethon wrapper (KF-18, D-256). Recording fake, no network."""

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tg_fakes import peer_for

from src import db
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.http import build_client
from src.sources.base import SourceContext, run_source
from src.sources.telegram_channels import TelegramChannelsSource, default_client_factory
from src.sources.telegram_live import TelegramListener
from src.sources.telegram_ro import ReadOnlyTelegram, ReadOnlyViolation, read_only

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
ENV = {
    "TELEGRAM_BOT_TOKEN": "t",
    "TELEGRAM_CHAT_ID": "1",
    "ANTHROPIC_API_KEY": "k",
    "TG_API_ID": "123",
    "TG_API_HASH": "fakehash",
}
BAD_METHODS = [
    "send_message",
    "send_file",
    "forward_messages",
    "delete_messages",
    "edit_message",
    "send_read_acknowledge",
]


class Recorder:
    """Records every call; the forbidden ones also land in `bad`."""

    def __init__(self):
        self.calls = []
        self.bad = []
        self.handlers = []
        for name in BAD_METHODS:
            setattr(self, name, self._bad(name))

    def _bad(self, name):
        async def f(*a, **k):
            self.bad.append(name)

        return f

    async def connect(self):
        self.calls.append("connect")

    async def disconnect(self):
        self.calls.append("disconnect")

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return True

    async def iter_dialogs(self):
        self.calls.append("iter_dialogs")
        yield SimpleNamespace(entity=SimpleNamespace(username="job_kz"))

    async def iter_messages(self, channel, limit=None, min_id=0):
        self.calls.append("iter_messages")
        yield SimpleNamespace(id=5, message="Intern ML\nCompany: Acme", date=NOW, fwd_from=None)

    async def get_input_entity(self, peer):
        self.calls.append("get_input_entity")
        return peer_for(peer) if isinstance(peer, str) else peer

    def add_event_handler(self, cb, event):
        self.calls.append("add_event_handler")
        self.handlers.append(cb)

    def remove_event_handler(self, cb):
        self.calls.append("remove_event_handler")

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        return "ok"


def make_request(name):
    return type(name, (), {})()


@pytest.mark.parametrize("name", BAD_METHODS)
def test_forbidden_methods_raise_without_reaching_the_client(name):
    rec = Recorder()
    ro = ReadOnlyTelegram(rec)
    with pytest.raises(ReadOnlyViolation):
        getattr(ro, name)
    assert rec.bad == []


@pytest.mark.parametrize(
    "name",
    ["JoinChannelRequest", "ImportChatInviteRequest", "LeaveChannelRequest", "SendMessageRequest"],
)
async def test_forbidden_requests_raise(name):
    rec = Recorder()
    with pytest.raises(ReadOnlyViolation):
        await ReadOnlyTelegram(rec)(make_request(name))
    assert rec.calls == []


@pytest.mark.parametrize("name", ["SearchRequest", "GetFullChannelRequest", "GetHistoryRequest"])
async def test_allowed_requests_pass_through(name):
    rec = Recorder()
    assert await ReadOnlyTelegram(rec)(make_request(name)) == "ok"
    assert rec.calls == [name]


def test_only_new_message_events_may_subscribe():
    rec = Recorder()
    ro = ReadOnlyTelegram(rec)
    ro.add_event_handler(print, make_request("NewMessage"))
    assert rec.handlers == [print]
    with pytest.raises(ReadOnlyViolation):
        ro.add_event_handler(print, make_request("ChatAction"))


def test_read_only_is_idempotent():
    ro = read_only(Recorder())
    assert read_only(ro) is ro


def test_default_factory_returns_wrapper(tmp_path):
    settings = Settings.from_env({**ENV, "TG_SESSION_PATH": str(tmp_path / "x" / "s")})
    # constructing a TelegramClient opens no connection until connect()
    assert isinstance(default_client_factory(settings), ReadOnlyTelegram)


async def test_polling_and_listener_use_only_allowed_calls():
    rec = Recorder()
    conn = db.connect(":memory:")
    db.migrate(conn)
    cfg = AppConfig.from_dict(
        {"sources": {"telegram_channels": {"enabled": True, "channels": ["job_kz"]}}}
    )
    settings = Settings.from_env(ENV)

    lst = TelegramListener(
        settings, cfg, conn, client_factory=lambda s: rec, require_session_file=False
    )
    assert await lst.start() is True
    assert isinstance(lst.client, ReadOnlyTelegram)
    await lst.reload()

    clock = FixedClock(NOW)
    ctx = SourceContext(
        http=build_client(conn, clock),
        settings=settings,
        cfg=cfg,
        clock=clock,
        log=logging.getLogger("t"),
    )
    src = TelegramChannelsSource(
        client_factory=lambda s: rec, pause_s=0, require_session_file=False
    )
    res = await run_source(src, ctx)
    assert res.ok
    assert "iter_messages" in rec.calls and "add_event_handler" in rec.calls
    assert rec.bad == []
    assert not any("Join" in c or "Import" in c or "Leave" in c for c in rec.calls)


# ----- KF-19: only public channels may be read -----


async def test_iter_messages_refuses_users_chats_and_private_peers():
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerSelf, InputPeerUser

    ro = read_only(Recorder())
    for bad in (InputPeerUser(1, 2), InputPeerChat(3), InputPeerSelf(), "me", "self", 12345, None):
        with pytest.raises(ReadOnlyViolation):
            ro.iter_messages(bad, limit=1)
    assert ro.iter_messages(InputPeerChannel(1, 2), limit=1) is not None
    assert ro.iter_messages("job_kz", limit=1) is not None


async def test_get_input_entity_refuses_a_user_result():
    from telethon.tl.types import InputPeerUser

    class UserRec(Recorder):
        async def get_input_entity(self, peer):
            return InputPeerUser(1, 2)

    with pytest.raises(ReadOnlyViolation):
        await read_only(UserRec()).get_input_entity("some_user")
