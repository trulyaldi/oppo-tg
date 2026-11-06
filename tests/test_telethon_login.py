import telethon

from src.tools import telethon_login


class FakeTG:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.calls = args, kwargs, []
        FakeTG.instances.append(self)

    async def start(self):
        self.calls.append("start")

    async def get_me(self):
        self.calls.append("get_me")
        return type("Me", (), {"first_name": "Test"})()

    async def disconnect(self):
        self.calls.append("disconnect")


def _env(monkeypatch, **kw):
    for k in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_PATH"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_main_requires_credentials(monkeypatch, capsys):
    _env(monkeypatch)
    assert telethon_login.main() == 2
    assert "TG_API_ID" in capsys.readouterr().err


def test_main_rejects_non_integer_api_id(monkeypatch, capsys):
    _env(monkeypatch, TG_API_ID="abc", TG_API_HASH="h")
    assert telethon_login.main() == 2
    assert "integer" in capsys.readouterr().err


def test_empty_session_path_uses_default(monkeypatch):
    _env(monkeypatch, TG_API_ID="12", TG_API_HASH="h", TG_SESSION_PATH="")
    seen = []

    async def fake_login(api_id, api_hash, session):
        seen.append((api_id, api_hash, session))

    monkeypatch.setattr(telethon_login, "login", fake_login)
    assert telethon_login.main() == 0
    assert seen == [(12, "h", "data/telethon.session")]


async def test_login_creates_parent_and_runs_client(monkeypatch, tmp_path):
    FakeTG.instances = []
    monkeypatch.setattr(telethon, "TelegramClient", FakeTG)
    path = tmp_path / "sub" / "dir" / "t.session"
    await telethon_login.login(7, "hash", str(path))
    assert path.parent.is_dir()
    c = FakeTG.instances[0]
    assert c.args == (str(path), 7, "hash")
    assert c.calls == ["start", "get_me", "disconnect"]
