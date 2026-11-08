import inspect
from pathlib import Path
from types import SimpleNamespace

from anthropic.resources.messages import AsyncMessages

from src import db
from src.config import Settings
from src.cv import review as rv
from src.cv import store
from src.main import _parser, run_cvreview

FX = Path(__file__).parent / "fixtures"
PARSE = (FX / "cv_parse_reply.json").read_text()
OK = (FX / "cv_review_reply_ok.json").read_text()


class Fake:
    def __init__(self):
        self.calls = []
        self.messages = self

    async def create(self, **kw):
        inspect.signature(AsyncMessages.create).bind(None, **kw)
        self.calls.append(kw)
        r = PARSE if len(self.calls) == 1 else OK
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=r)])


class FakeBot:
    def __init__(self):
        self.messages = []
        self.documents = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, **kw):
        self.documents.append((chat_id, document))


def settings(tmp_path):
    return Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "1:t",
            "TELEGRAM_CHAT_ID": "42",
            "ANTHROPIC_API_KEY": "k",
            "DB_PATH": str(tmp_path / "scratch.db"),
            "OPPO_CONFIG": str(FX / "config_repo_snapshot.toml"),
        }
    )


async def test_cli_with_cv_file_sends_summary_and_document(tmp_path, capsys):
    store.on_new_cv_version.clear()
    store.on_forget_cv.clear()
    rv.register_hooks()
    bot = FakeBot()
    code = await run_cvreview(settings(tmp_path), str(FX / "cv_fake.txt"), bot, Fake())
    assert code == 0
    assert len(bot.messages) == 1 and len(bot.documents) == 1
    assert bot.messages[0][0] == "42"
    out = capsys.readouterr().out
    assert "cv review v1" in out and "latency benchmark" not in out
    assert sorted(p.suffix for p in tmp_path.iterdir()).count(".md") == 0
    c = db.connect(tmp_path / "scratch.db")
    assert c.execute("SELECT COUNT(*) FROM cv_reviews").fetchone()[0] == 1
    c.close()


async def test_cli_no_cv(tmp_path):
    bot = FakeBot()
    assert await run_cvreview(settings(tmp_path), None, bot, Fake()) == 1
    assert not bot.messages and not bot.documents


def test_parser_accepts_cvreview():
    a = _parser().parse_args(["cvreview", "--cv-file", "x.txt"])
    assert a.command == "cvreview" and a.cv_file == "x.txt"
