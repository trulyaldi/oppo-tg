import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot
from aiogram.types import Chat, Document, Message, Update, User

from src import db
from src.bot.app import build_dispatcher
from src.bot.outreach_handlers import UNSUPPORTED, on_document
from src.bot.pending import peek_pending, set_pending
from src.clock import FixedClock
from src.config import AppConfig, Settings
from src.cv.extract import CvInputError, pdf_to_text
from src.cv.store import current_cv
from tests.test_bot_cv import REPLY, FakeClient, Msg

FIX = Path(__file__).parent / "fixtures"
PDF = (FIX / "cv_fake.pdf").read_bytes()
SCANNED = (FIX / "cv_scanned_fake.pdf").read_bytes()
CFG = AppConfig.load(FIX / "config_repo_snapshot.toml")
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
SETTINGS = Settings.from_env(
    {"TELEGRAM_BOT_TOKEN": "1:t", "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "k"}
)


class DocMsg(Msg):
    def __init__(self, name="cv.pdf", mime="application/pdf", size=1000):
        super().__init__()
        self.document = SimpleNamespace(file_name=name, mime_type=mime, file_size=size)


class FakeBot:
    def __init__(self, data=PDF):
        self.data = data
        self.downloads = 0

    async def download(self, doc, destination=None, **kw):
        self.downloads += 1
        destination.write(self.data)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.migrate(c)
    return c


@pytest.fixture
def clock():
    return FixedClock(NOW)


def test_pdf_to_text_fixture():
    t = pdf_to_text(PDF)
    assert "Alex Example" in t and "FAKE FIXTURE" in t


def test_pdf_errors():
    with pytest.raises(CvInputError, match="No text"):
        pdf_to_text(SCANNED)
    with pytest.raises(CvInputError):
        pdf_to_text(b"not a pdf")
    with pytest.raises(CvInputError, match="pages"):
        pdf_to_text(PDF, max_pages=0)
    with pytest.raises(CvInputError):
        pdf_to_text(b"%PDF-1.4 garbage")


def test_encrypted_pdf():
    import io

    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(PDF)))
    w.encrypt("pw")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(CvInputError, match="encrypted"):
        pdf_to_text(buf.getvalue())


async def test_happy_path_stores_pdf(conn, clock, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    set_pending(conn, "cv", None, NOW, 15)
    m, bot = DocMsg(), FakeBot()
    await on_document(m, conn, clock, CFG, FakeClient(REPLY), bot)
    assert "CV v1 saved" in m.sent[0]
    assert current_cv(conn)["source_kind"] == "pdf"
    assert list(tmp_path.iterdir()) == []


async def test_too_big_no_download(conn, clock):
    set_pending(conn, "cv", None, NOW, 15)
    m, bot = DocMsg(size=6_000_000), FakeBot()
    await on_document(m, conn, clock, CFG, FakeClient(), bot)
    assert "too big" in m.sent[0] and bot.downloads == 0


async def test_unsupported_and_scanned(conn, clock):
    set_pending(conn, "cv", None, NOW, 15)
    m, bot = DocMsg("cv.docx", "application/vnd.x"), FakeBot()
    await on_document(m, conn, clock, CFG, FakeClient(), bot)
    assert m.sent == [UNSUPPORTED] and bot.downloads == 0
    m2 = DocMsg()
    await on_document(m2, conn, clock, CFG, FakeClient(), FakeBot(SCANNED))
    assert "No text" in m2.sent[0] and current_cv(conn) is None
    assert peek_pending(conn, NOW) is not None  # retry possible


async def test_pdf_by_extension_only(conn, clock):
    set_pending(conn, "cv", None, NOW, 15)
    m = DocMsg("CV.PDF", "application/octet-stream")
    await on_document(m, conn, clock, CFG, FakeClient(REPLY), FakeBot())
    assert "CV v1 saved" in m.sent[0]


def doc_update(chat_id, name="cv.pdf", mime="application/pdf"):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=NOW,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=chat_id, is_bot=False, first_name="x"),
            document=Document(
                file_id="f", file_unique_id="u", file_name=name, mime_type=mime, file_size=10
            ),
        ),
    )


async def dispatch(conn, clock, chat_id, monkeypatch, **kw):
    sent, downloads = [], []

    async def fake_call(self, method, *a, **k):
        sent.append(method)
        return None

    async def fake_download(self, doc, destination=None, **k):
        downloads.append(doc)
        destination.write(PDF)

    monkeypatch.setattr(Bot, "__call__", fake_call)
    monkeypatch.setattr(Bot, "download", fake_download)
    dp = build_dispatcher(conn, SETTINGS, CFG, clock, FakeClient(REPLY))
    bot = Bot("1:t")
    await dp.feed_update(bot, doc_update(chat_id, **kw))
    await bot.session.close()
    return sent, downloads


async def test_dispatcher_ignored_when_not_pending(conn, clock, monkeypatch):
    sent, dl = await dispatch(conn, clock, 42, monkeypatch)
    assert sent == [] and dl == []


async def test_dispatcher_other_chat_no_download(conn, clock, monkeypatch):
    set_pending(conn, "cv", None, NOW, 15)
    sent, dl = await dispatch(conn, clock, 99, monkeypatch)
    assert sent == [] and dl == [] and peek_pending(conn, NOW) is not None


async def test_dispatcher_ingests_pdf(conn, clock, monkeypatch):
    set_pending(conn, "cv", None, NOW, 15)
    sent, dl = await dispatch(conn, clock, 42, monkeypatch)
    assert len(dl) == 1 and "CV v1 saved" in sent[0].text
    assert current_cv(conn)["source_kind"] == "pdf"


async def test_canary_not_logged(conn, clock, caplog):
    caplog.set_level(logging.DEBUG)
    set_pending(conn, "cv", None, NOW, 15)
    await on_document(DocMsg(), conn, clock, CFG, FakeClient(REPLY), FakeBot())
    assert "Alex Example" not in caplog.text and "FAKE FIXTURE" not in caplog.text
